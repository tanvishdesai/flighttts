# Mauritius Flight Optimizer

Finds the cheapest Ahmedabad to Mauritius itineraries using the Skyscanner web API.

## Quick start

```bash
pip install requests
python kaggle_optimizer.py
```

## Run modes

```bash
python kaggle_optimizer.py                  # full pipeline
python kaggle_optimizer.py --phase monitor  # daily price tracking
python kaggle_optimizer.py --resume         # continue after interrupt
python kaggle_optimizer.py --skip-gateway --skip-oneway
```

## Output

Results in `results/`: best_itineraries CSV/JSON, price_history.json, checkpoints.

## Kaggle

Enable Internet, run `!python kaggle_optimizer.py`.

## Repo layout

- `kaggle_optimizer.py` — main optimizer
- `flight_search.py` — generic search engine
- `dev/` — HAR analysis scripts
