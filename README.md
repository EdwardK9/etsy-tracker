# Etsy Tracker

Self-hosted tracker for a small Etsy 3D-printing shop: products, print batches and stock, sales, expenses, price/cost history, and a profit dashboard. Filament cost is pulled from [Bambuddy](https://github.com/maziggy/bambuddy) via its API.

Flask + SQLite + vanilla JS. Data lives in `/data/etsy.db` (mount a volume there).

## Deploy with Portainer
Stacks → Add stack → Repository → this repo, compose path `docker-compose.yml`. Turn on automatic updates to redeploy on each push. Then open `http://<server>:5075` and set up Bambuddy under Settings.

## Run locally
    pip install flask && DB_PATH=./etsy.db python app.py

## Run the tests
    pip install flask pytest && pytest -q

The tests cover fees, costs and profit, stock and the order pipeline, Print today, supplies, Etsy order sync and the CSV import. Etsy is faked, so they need no account or network.
