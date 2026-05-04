# Lottery Predictor

This repository is the **experimental comparison version** of `lottery-ai-top3`.

It keeps two parallel methods:

- **V1 stable scoring**: frequency, recency, omission, transition, trend shape, and weak pre-draw signals.
- **V2 constraint sampling**: active digit zones, sum/span/odd-big constraints, momentum pairs, weighted sampling, and diversified top-3 selection.

`lottery-ai-top3` focuses on multi-model ensemble and rolling model-weight backtests. This repository deliberately keeps a different mechanism so the two projects can be compared instead of becoming copies of each other.

## Cloud Refresh

GitHub Actions runs every 10 minutes.

- Before 22:00 Beijing time: refresh predictions.
- After 22:00 Beijing time: refresh predictions and run post-draw review.
- Generated reports are published to the `gh-pages` branch.

## Mobile Page

After GitHub Pages is enabled, open:

`https://wzt0609.github.io/lottery-predictor/`

## Local Commands

```bash
python lottery_predictor.py predict
python lottery_predictor_v2.py predict
python lottery_predictor.py post-draw
python lottery_predictor_v2.py post-draw
```

Lottery draws are random. These reports are for statistical logging and entertainment only, not guaranteed winning numbers or investment advice.
