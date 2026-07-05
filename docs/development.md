# Development

## Repository layout

```
ha-smartgridready/
├── smartgridready/        # the add-on itself (HA Supervisor picks this up)
│   ├── config.yaml        # add-on manifest
│   ├── build.yaml         # multi-arch base image map
│   ├── Dockerfile         # image build
│   ├── requirements.txt   # pinned Python deps
│   ├── run.sh             # container entry point
│   ├── logo.svg           # SGr logo
│   └── src/               # Python source
│       ├── main.py            # event loop
│       ├── options.py         # /data/options.json reader
│       ├── config_loader.py   # user YAML loader
│       ├── ha_client.py       # HA Supervisor REST client
│       ├── sgr_service.py     # sgr-commhandler wrapper
│       ├── rules_engine.py    # DSL + evaluator + audit
│       ├── virtual_devices.py # non-SGr devices piloted via HA services
│       ├── pv_forecast.py     # self-computed PV forecast (Open-Meteo)
│       ├── optimizer.py       # opt-in predictive-dispatch MILP
│       ├── mqtt_discovery.py  # HA MQTT entity bridge
│       ├── webui.py           # FastAPI ingress UI
│       ├── templates/         # Jinja2 HTML
│       └── static/            # CSS
├── tests/                 # pytest suite (no live broker / no live HA needed)
├── docs/                  # this folder
├── examples/              # reference user config
├── repository.yaml        # add-on store metadata
├── pyproject.toml         # pytest + ruff config
└── README.md
```

## Running the tests locally

```bash
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r smartgridready/requirements.txt
pip install -r tests/requirements.txt
pytest
```

`sgr-commhandler` is a heavy dependency. The tests do **not** import
it; the modules under test guard the import and degrade gracefully if
it is missing, which makes CI fast and laptop-friendly.

## Building the image locally

You normally do not need to build the image yourself — Home Assistant
Supervisor does it from `config.yaml` + `Dockerfile` when you install
the add-on. If you want to:

```bash
docker buildx build \
  --build-arg BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.19 \
  -t local/smartgridready:dev \
  smartgridready/
```

## Linting and formatting

```bash
ruff check smartgridready/src tests/
ruff format smartgridready/src tests/
```

(`ruff` is not part of the runtime requirements; install it
separately: `pip install ruff`.)

## Live development without a real HA

The simplest dev loop is to run the Python module straight from your
machine and point it at a fake config file:

```bash
mkdir -p /tmp/sgr-dev/addon_config /tmp/sgr-dev/share

cat > /tmp/sgr-dev/options.json <<EOF
{
  "config_path": "/tmp/sgr-dev/addon_config/config.yaml",
  "evaluation_interval": 60,
  "log_level": "debug",
  "mqtt_discovery": false,
  "mqtt_prefix": "smartgridready",
  "share_path": "/tmp/sgr-dev/share"
}
EOF

cp examples/config.yaml /tmp/sgr-dev/addon_config/config.yaml

SGR_OPTIONS_FILE=/tmp/sgr-dev/options.json \
PYTHONPATH=smartgridready \
python -m src.main
```

You will see one evaluation cycle every 60 seconds, the ingress UI on
http://localhost:8099 (no Supervisor proxy in front of it for local
dev — talk to it directly), and an audit log being written to
`/tmp/sgr-dev/share/audit.json`.

Without a `SUPERVISOR_TOKEN`, the HA client returns empty state and
every rule will skip with `no_condition_matched` — that is expected
and lets you sanity-check the wiring before connecting to a real HA.
