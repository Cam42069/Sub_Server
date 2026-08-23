"""Example transform functions.

Every function receives ``data`` first: ``data.names`` lists the variables the
plot selected, ``data.v(name)`` gives their values and ``data.t(name)`` their
timestamps.  Extra keyword arguments come from the profile's function ``args``.
"""

import numpy as np


def rolling_mean(data, window=30):
    """Smooth each selected variable with a centred rolling mean.

    window: number of samples to average over.
    """
    window = max(1, int(window))
    output = {}
    for name in data.names:
        values = data.v(name)
        if values.size == 0:
            continue
        # Pad the edges so the smoothed trace spans the same time range.
        padded = np.pad(values, (window // 2, window - 1 - window // 2), mode="edge")
        kernel = np.ones(window) / window
        output[f"{name} (mean {window})"] = np.convolve(padded, kernel, mode="valid")[: values.size]
    return output


def difference(data):
    """Plot the difference between the first two selected variables."""
    if len(data.names) < 2:
        raise ValueError("Select at least two variables to subtract.")
    first, second = data.names[0], data.names[1]
    t_a, v_a = data.series(first)
    t_b, v_b = data.series(second)
    if t_a.size == 0 or t_b.size == 0:
        return {}
    # Resample the second variable onto the first one's timestamps so the
    # subtraction is well defined even at different sample rates.
    aligned = np.interp(t_a, t_b, v_b)
    return {f"{first} - {second}": (t_a, v_a - aligned)}


def summary(data):
    """Return the mean, minimum and maximum of the first variable.

    Scalars are drawn as labelled horizontal reference lines.
    """
    if not data.names:
        return {}
    values = data.v(data.names[0])
    if values.size == 0:
        return {}
    return {
        "mean": float(np.mean(values)),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
    }


def rate_of_change(data, per_seconds=60.0):
    """Numerical derivative of each variable, scaled to units per `per_seconds`."""
    per_seconds = float(per_seconds) or 1.0
    output = {}
    for name in data.names:
        times, values = data.series(name)
        if values.size < 2:
            continue
        derivative = np.gradient(values, times) * per_seconds
        output[f"d({name})/dt"] = (times, derivative)
    return output


def normalise(data):
    """Rescale every variable to 0-1 so differently-scaled traces can share an axis."""
    output = {}
    for name in data.names:
        values = data.v(name)
        if values.size == 0:
            continue
        low, high = float(np.min(values)), float(np.max(values))
        span = high - low
        output[f"{name} (norm)"] = (values - low) / span if span else np.zeros_like(values)
    return output


def histogram(data, bins=40):
    """Return a matplotlib histogram of the first selected variable.

    Requires matplotlib on the server; shown as an image in place of the plot.
    """
    import matplotlib
    matplotlib.use("Agg", force=False)
    from matplotlib.figure import Figure

    if not data.names:
        raise ValueError("Select a variable first.")
    name = data.names[0]
    values = data.v(name)

    figure = Figure(figsize=(6.4, 3.6), facecolor="#1e1e1e")
    axes = figure.add_subplot(111, facecolor="#252525")
    axes.hist(values, bins=int(bins), color="#4fc3f7", edgecolor="#1e1e1e")
    axes.set_title(f"Distribution of {name}", color="#f0f0f0")
    axes.set_xlabel("value", color="#c8c8c8")
    axes.set_ylabel("count", color="#c8c8c8")
    axes.tick_params(colors="#c8c8c8")
    for spine in axes.spines.values():
        spine.set_color("#4a4a4a")
    axes.grid(True, color="#3a3a3a", linestyle=":")
    return figure
