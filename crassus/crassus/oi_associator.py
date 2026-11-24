"""Pure Python forward inference for the OA-12 opening OI associators."""

import math
import struct
from datetime import date

ARCHITECTURES = ("ridge", "forest", "prototype", "pls")
SCHEMA = "qqq-opening-oi-path/v1"
OFFSETS = range(-3, 4)


def features(spot, legs):
    if not math.isfinite(spot) or spot <= 0:
        raise ValueError("Invalid opening spot")
    center = math.floor(spot + 0.5)
    wanted = {center + offset for offset in OFFSETS}
    ladder = {}
    for leg in legs:
        strike = leg["strike"]
        if strike not in wanted:
            continue
        if strike in ladder:
            raise ValueError("Duplicate opening strike")
        values = (leg["call"], leg["put"])
        if any(
            type(x) not in (int, float) or not math.isfinite(x) or x < 0 or int(x) != x
            for x in values
        ):
            raise ValueError("Missing or invalid opening OI")
        ladder[strike] = values
    if set(ladder) != wanted:
        raise ValueError("Incomplete seven-strike opening ladder")
    return [math.log1p(qty) for k in sorted(wanted) for qty in ladder[k]] + [
        spot - center
    ]


def dot(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def forecast(bundle, architecture, session, spot, legs):
    if bundle["schema"] != SCHEMA or architecture not in ARCHITECTURES:
        raise ValueError("Unsupported opening OI model")
    if date.fromisoformat(bundle["trained_through"]) >= date.fromisoformat(session):
        raise ValueError("Model requires a strictly later decision session")
    x = [
        (value - mean) / scale
        for value, mean, scale in zip(
            features(spot, legs), bundle["mean"], bundle["scale"], strict=True
        )
    ]
    model = bundle["models"][architecture]
    if architecture in ("ridge", "pls"):
        y = [
            offset + dot(weights, x)
            for offset, weights in zip(
                model["intercept"], model["weights"], strict=True
            )
        ]
    elif architecture == "prototype":
        nearest = min(
            range(len(model["centers"])),
            key=lambda k: sum(
                (a - b) ** 2 for a, b in zip(model["centers"][k], x, strict=True)
            ),
        )
        y = model["paths"][nearest]
    else:
        paths = []
        for tree in model["trees"]:
            node = 0
            while tree["left"][node] != -1:
                # sklearn's tree predictor converts features to float32 before splitting.
                value = struct.unpack("f", struct.pack("f", x[tree["feature"][node]]))[
                    0
                ]
                node = (
                    tree["left"][node]
                    if value <= tree["threshold"][node]
                    else tree["right"][node]
                )
            paths.append(tree["values"][node])
        y = [sum(col) / len(paths) for col in zip(*paths, strict=True)]
    if len(y) != 3 or not all(math.isfinite(value) for value in y):
        raise ValueError("Invalid forecast path")
    return tuple(y)
