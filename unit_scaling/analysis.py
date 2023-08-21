# Copyright (c) 2023 Graphcore Ltd. All rights reserved.

"""Tools for analysing scale (and other metrics) within PyTorch models."""

import colorsys
import logging
import re
from math import isnan
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

import matplotlib  # type: ignore[import]
import matplotlib.colors  # type: ignore[import]
import matplotlib.pyplot as plt  # type: ignore[import]
import pandas as pd
import seaborn as sns  # type: ignore[import]
from datasets import load_dataset  # type: ignore[import]
from torch import Tensor, nn
from torch.fx.graph import Graph
from torch.fx.node import Node

from ._internal_utils import generate__all__
from .transforms import (
    Metrics,
    prune_non_float_tensors,
    prune_same_scale_tensors,
    track_scales,
)

if TYPE_CHECKING:  # pragma: no cover
    from transformers.tokenization_utils_base import (  # type: ignore
        PreTrainedTokenizerBase,
    )

logger = logging.getLogger(__name__)


def _example_seqs(
    batch_size: int,
    min_seq_len: int,
    dataset_path: str = "wikitext",
    dataset_name: str = "wikitext-103-v1",
    shuffle_buffer_size: int = 10_000,
    seed: int = 1472,
) -> List[str]:
    dataset = load_dataset(dataset_path, dataset_name, split="test", streaming=True)
    shuffled_dataset = dataset.shuffle(seed=seed, buffer_size=shuffle_buffer_size)
    filtered_dataset = shuffled_dataset.filter(lambda d: len(d["text"]) > min_seq_len)
    batch = filtered_dataset.take(batch_size)
    return [d["text"] for d in batch]


def _create_batch(
    tokenizer: "PreTrainedTokenizerBase",
    seqs: List[str],
    seq_len: int,
) -> Tuple[Tensor, Tensor, Tensor]:
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    out = tokenizer(
        seqs, max_length=seq_len + 1, truncation=True, return_tensors="pt", padding=True
    )
    input_idxs = out["input_ids"][:, :seq_len].clone()
    attn_mask = out["attention_mask"][:, :seq_len].clone()
    labels = out["input_ids"][:, 1 : seq_len + 1].clone()
    return input_idxs, attn_mask, labels


def example_batch(
    tokenizer: "PreTrainedTokenizerBase",
    batch_size: int,
    seq_len: int,
    dataset_path: str = "wikitext",
    dataset_name: str = "wikitext-103-v1",
    shuffle_buffer_size: int = 10_000,
    seed: int = 1472,
) -> Tuple[Tensor, Tensor, Tensor]:
    """Generates a batch of token IDs from a given dataset, along with an attention mask
    and labels (just the shifted token IDs).

    Args:
        tokenizer (PreTrainedTokenizerBase): the tokenizer applied to the text data.
        batch_size (int): the batch size of the returned tensor.
        seq_len (int): the sequence length (number of IDs) of the returned tensor.
        dataset_path (str, optional): huggingface path of the dataset to use for
            visualisation. Defaults to "wikitext".
        dataset_name (str, optional): huggingface name of the dataset to use for
            visualisation. Defaults to "wikitext-103-v1".
        shuffle_buffer_size (int, optional): the tokenized data is a random sample from
            a chunk of the full dataset. This determines the chunk size.
            Defaults to 10_000.
        seed (int, optional): shuffle seed. Defaults to 1472.

    Returns:
        Tuple[Tensor]: a tuple of (input_idxs, attn_mask, labels)
    """
    seqs = _example_seqs(
        batch_size, seq_len * 4, dataset_path, dataset_name, shuffle_buffer_size, seed
    )
    return _create_batch(
        tokenizer,
        seqs,
        seq_len,
    )


def graph_to_dataframe(g: Graph) -> pd.DataFrame:
    """Converts a :class:`torch.fx.Graph` with annotated
    :class:`unit_scaling.transforms.Metrics` into a :class:`pandas.DataFrame`.

    This graph is indended to have been generated by applying
    :func:`unit_scaling.transforms.track_scales` to an arbitrary
    :class:`torch.nn.Module`, running a forward (and possibly backward) pass,
    then calling the `module.scales_graph()` function.

    The resulting dataframe contains all the metrics information for the module,
    and is used internally by the :func:`unit_scaling.analysis.plot` function.

    Args:
        g (Graph): the input graph.

    Returns:
        pd.DataFrame: the metrics dataframe.
    """
    columns = [
        "layer",
        "weight tensor",
        "direction",
        "tensor type",
    ] + Metrics.full_names()
    data = []
    for n in g.nodes:
        # 'output' has to be kept from previous stages to keep fx happy. We drop it here
        if n.name == "output":
            continue
        for direction in ["fwd", "bwd"]:
            tensor_type_prefix = "" if direction == "fwd" else "grad_"
            tensor_type_suffix = "w" if n.meta["requires_grad"] else "x"
            row_data = [
                n.meta["clean_name"],
                n.meta["requires_grad"],
                direction,
                tensor_type_prefix + tensor_type_suffix,
            ]
            for m in Metrics.names():
                directional_metrics = getattr(n.meta["metrics"], direction, None)
                if directional_metrics is not None:
                    v = getattr(directional_metrics, m)
                else:
                    v = None  # pragma: no cover
                row_data.append(v)
            data.append(row_data)

    return pd.DataFrame.from_dict(
        {i: row for i, row in enumerate(data)},
        orient="index",
        columns=columns,
    )


def plot(
    g: Graph,
    title: str = "",
    metric: str = "mean_abs",
    prune_same_scale: bool = True,
    show_arrows: bool = True,
    show_error_bars: bool = True,
    show_zero_tensors: bool = False,
    xmin: Optional[float] = None,
    xmax: Optional[float] = None,
) -> matplotlib.axes.Axes:
    """Generate a :mod:`matplotlib` plot visualising the scales in the forward (and
    optionally backward) pass of all tensors in an FX graph.

    The input graph is intended to have been generated by applying
    :func:`unit_scaling.transforms.track_scales` to an arbitrary
    :class:`torch.nn.Module`, running a forward (and possibly backward) pass,
    then calling the `module.scales_graph()` function:

    .. code-block:: python

        from unit_scaling.transforms import track_scales
        from unit_scaling.analysis import plot

        inpt = ...
        model = ...

        model = track_scales(model)
        loss = model(inpt)
        loss.backward()

        graph = model.scales_graph()
        plot(graph)

    Operations that don't output floating-point tensors are automatically pruned from
    the visualised graph, as they are deemed unlikely to be relevant from the
    perspective of model numerics.

    Faint coloured horizontal lines for each row represent error bars indicating
    the maximum and minimum values seen in each tensor during tracking.

    Args:
        g (Graph): the graph to visualise.
        title (str, optional): title for the generated plot. Defaults to "".
        metric (str, optional): the metric to show on the x-axis. Can be any of:
            ("mean_abs", "abs_mean", "std", "abs_max", "abs_min", "numel").
            Defaults to "mean_abs".
        prune_same_scale (bool, optional): prune operations that don't change the scale
            of their input tensors. In practice this means that views / reshapes are not
            shown, making the resulting visualisation clearer. Defaults to True.
        show_arrows (bool, optional): show arrows between operations,
            denoting dependencies. Defaults to True.
        show_error_bars (bool, optional): show max/min error bars. Defaults to True.
        xmin (Optional[float], optional): the minimum x-value to display.
            Defaults to None.
        xmax (Optional[float], optional): the maximum x-value to display.
            Defaults to None.

    Returns:
        matplotlib.axes.Axes: the axes representing the generated plot.
    """
    assert metric in Metrics.names() + Metrics.full_names(), (
        f"metric '{metric}' must be one of {Metrics.names()} (these correspond to"
        f" {Metrics.full_names()})"
    )
    full_metric = Metrics.get_full_name(metric)

    g = prune_non_float_tensors(g)
    if prune_same_scale:
        g = prune_same_scale_tensors(g)

    df = graph_to_dataframe(g)

    plot_height = len(df["layer"].unique())
    plt.figure(figsize=(10, plot_height / 4))

    colors = sns.color_palette("colorblind")
    sns.set_palette(colors)

    sns.set_theme()
    p = sns.lineplot(
        data=df,
        x=full_metric,
        y="layer",
        hue="direction",
        hue_order=["fwd", "bwd"],
        style="weight tensor",
        style_order=[False, True],
        dashes=[(0, 1), (0, 1)],
        markers=[".", "v"],
        markersize=9,
        estimator=None,
        orient="y",
    )

    p.set_ylim(plot_height, -1)
    plt.xscale("log", base=2)
    p.xaxis.set_ticks_position("top")
    p.xaxis.set_label_position("top")
    p.xaxis.grid(False)
    if title:
        p.set_title(title, fontweight="bold")

    label_map = {
        "fwd": "forward pass",
        "bwd": "backward pass",
        "False": "non-weight tensor",
        "True": "weight tensor",
    }
    new_legend_labels = {
        label_map[l]: h
        for h, l in zip(*p.get_legend_handles_labels())
        if l in label_map
    }
    p.legend(
        new_legend_labels.values(), new_legend_labels.keys(), loc="upper right"
    ).set_title("")

    def _rename(s: str) -> str:
        s = re.sub(r"(^|_)\d+", "", s)
        s = s.replace("self_", "")
        s = s.replace("transformer_h_", "")
        s = s.replace("transformer_", "")
        return s

    p.set_yticklabels([_rename(item.get_text()) for item in p.get_yticklabels()])

    plt.axvline(2**-14, color="grey", dashes=(3, 1))
    plt.axvline(2**-7, color="grey", dashes=(1, 3))
    plt.axvline(240, color="grey", dashes=(1, 3))
    plt.axvline(2**16, color="grey", dashes=(3, 1))
    plt.text(
        2**-14,
        plot_height + 0.2,
        "FP16 min,\nFP8 E5 min\n(normal)",
        ha="center",
        va="top",
        size=9,
    )
    plt.text(
        2**-7,
        plot_height + 0.2,
        "FP8 E4 min\n(normal)",
        ha="center",
        va="top",
        size=9,
    )
    plt.text(
        240,
        plot_height + 0.2,
        "FP8 E4 max",
        ha="center",
        va="top",
        size=9,
    )
    plt.text(
        2**16,
        plot_height + 0.2,
        "FP16 max,\nFP8 E5 max",
        ha="center",
        va="top",
        size=9,
    )

    # Cycle through the graph's nodes and give each an index (for the y-axis)
    i = 0
    node_idxs = {}
    for node in g.nodes:
        if node.name != "output":
            name = node.meta["clean_name"]
            if name not in node_idxs:
                node_idxs[name] = i
                i += 1

    min_scale, max_scale = plt.gca().get_xlim()
    if xmin is not None:
        min_scale = xmin
    if xmax is not None:
        max_scale = xmax

    def lighten_color(
        color: Tuple[float, float, float], l_degree: float, s_degree: float
    ) -> Tuple[float, float, float]:
        r, g, b = matplotlib.colors.to_rgb(color)
        h, l, s = colorsys.rgb_to_hls(r, g, b)
        new_l = 1 - l_degree * (1 - l)
        new_s = s_degree * s
        return colorsys.hls_to_rgb(h, new_l, new_s)

    light_colors = [lighten_color(c, l_degree=0.35, s_degree=0.45) for c in colors]

    def draw_error_bar(node: Node, direction: str) -> None:
        metrics = node.meta["metrics"]
        if direction == "bwd" and metrics.bwd is None:  # pragma: no cover
            return

        directional_metrics = getattr(metrics, direction)
        x1, x2 = directional_metrics.abs_min, directional_metrics.abs_max
        y = node_idxs[node.meta["clean_name"]] + (-0.1 if direction == "fwd" else 0.1)
        color = light_colors[0 if direction == "fwd" else 1]
        plt.plot(
            [x1, x2],
            [y, y],
            color=color,
            linestyle="-",
            linewidth=1,
            marker="",
            zorder=1,
        )
        for x in [x1, x2]:
            plt.plot(
                [x, x],
                [y - 0.2, y + 0.2],
                color=color,
                linestyle="-",
                linewidth=1,
                marker="",
                zorder=1,
            )
        plt.gca().set_xlim(min_scale, max_scale)

    def draw_arrow(node_a: Node, node_b: Node, direction: str) -> None:
        a_metrics = node_a.meta["metrics"]
        b_metrics = node_b.meta["metrics"]
        if direction == "bwd" and (  # pragma: no cover
            a_metrics.bwd is None or b_metrics.bwd is None
        ):
            return  # pragma: no cover

        a_x = getattr(getattr(a_metrics, direction), metric)
        b_x = getattr(getattr(b_metrics, direction), metric)
        a_y = node_idxs[node_a.meta["clean_name"]]
        b_y = node_idxs[node_b.meta["clean_name"]]

        annotation = ""
        if a_x == 0 or isnan(a_x):  # pragma: no cover
            a_x = min_scale
        if isnan(a_x):  # pragma: no cover
            logging.warning(f"Node '{node_a.meta['clean_name']}' is NaN. Plotting as 0")
            a_x = min_scale
        if b_x == 0:  # pragma: no cover
            b_x = min_scale
            annotation = "0"
        if isnan(b_x):  # pragma: no cover
            logging.warning(f"Node '{node_b.meta['clean_name']}' is NaN. Plotting as 0")
            b_x = min_scale
            annotation = "0"

        if direction == "fwd":
            color = colors[0]
        else:
            assert direction == "bwd", direction
            color = colors[1]
            a_x, a_y, b_x, b_y = b_x, b_y, a_x, a_y

        if annotation == "0" and not show_zero_tensors:
            return

        plt.annotate(
            annotation,
            color=color,
            va="center",
            xy=((a_x, a_y)),
            xytext=((b_x, b_y)),
            arrowprops=dict(arrowstyle="->", color=color),
        )

    if show_arrows:
        for n in g.nodes:
            if n.name != "output":
                for direction in ["fwd", "bwd"]:
                    for arg in n.args:
                        if isinstance(arg, Node):
                            draw_arrow(n, arg, direction)

    if show_error_bars:
        for n in g.nodes:
            if n.name != "output":
                for direction in ["fwd", "bwd"]:
                    draw_error_bar(n, direction)

    return p


def visualiser(
    model: nn.Module,
    tokenizer: "PreTrainedTokenizerBase",
    batch_size: int,
    seq_len: int,
    backward: bool = True,
    dataset_path: str = "wikitext",
    dataset_name: str = "wikitext-103-v1",
    **plot_kwargs: Any,
) -> matplotlib.axes.Axes:
    """[Experimental] Generate a plot visualising the scales in the forward (and
    optionally backward) pass of all tensors in an arbitrary :class:`torch.nn.Module`.

    This is a convenience method which combines
    :func:`unit_scaling.analysis.example_batch`,
    :func:`unit_scaling.transforms.track_scales` and
    :func:`unit_scaling.analysis.plot`.

    Warning: this method is experimental and may not work for a wide range of
    models. It currently only supports models that use the following interface:

    .. code-block:: python

        output, loss = model(inputs, labels)

    Future versions will support standard huggingface interfaces. For now we recommend
    users with models providing different interfaces to re-implement this method for
    their use case, based on the following template:

    .. code-block:: python

        inputs, attn_mask, labels = example_batch(
            tokenizer, batch_size, seq_len, dataset_path, dataset_name
        )
        tracked_model = track_scales(model)
        loss = ... # code to call model with (inputs, attn_mask, labels), returning loss
        if backward:
            loss.backward()
        graph = tracked_model.scales_graph()
        return plot(graph, **plot_kwargs)

    Operations that don't output floating-point tensors are automatically pruned from
    the visualised graph, as they are deemed unlikely to be relevant from the
    perspective of model numerics.

    Faint coloured horizontal lines for each row represent error bars indicating
    the maximum and minimum values seen in each tensor during tracking.

    Args:
        model (nn.Module): the model to visualise
        tokenizer (PreTrainedTokenizerBase): the tokenizer corresponding to the model.
        batch_size (int): the batch size for the visualisation
        seq_len (int): the sequence length for the visualisation
        backward (bool, optional): visualise scales in the backward pass.
            Defaults to True.
        dataset_path (str, optional): huggingface path of the dataset to use for
            visualisation. Defaults to "wikitext".
        dataset_name (str, optional): huggingface name of the dataset to use for
            visualisation. Defaults to "wikitext-103-v1".
        plot_kwargs (Any): keyword args passed to :func:`unit_scaling.analysis.plot`.

    Returns:
        matplotlib.axes.Axes: the axes representing the generated plot.
    """
    inputs, attn_mask, labels = example_batch(
        tokenizer, batch_size, seq_len, dataset_path, dataset_name
    )
    tracked_model = track_scales(model.to("cpu"))
    _, loss = tracked_model(inputs, labels)
    if backward:
        loss.backward()
    graph = tracked_model.scales_graph()  # type: ignore[operator]
    return plot(graph, **plot_kwargs)


__all__ = generate__all__(__name__)
