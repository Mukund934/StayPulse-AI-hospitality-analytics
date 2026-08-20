# StayPulse — public presentation layer

A single static page. No build step, no dependencies, no server, **no environment
variables**.

That is deliberate. The analytics engine is Python + PostgreSQL and is not a web
application; wrapping it in a fake Python entrypoint purely to satisfy a platform
detector would be dishonest architecture. This directory is a separate,
independently deployable presentation surface over already-generated artifacts.

Because it ships no credentials and reads no database, there is nothing here that
can leak.

## Deploying on Vercel

| Setting | Value |
|---|---|
| Root Directory | `web` |
| Framework Preset | **Other** |
| Build Command | *(leave empty)* |
| Output Directory | *(leave empty)* |
| Install Command | *(leave empty)* |
| Environment Variables | **none** |

Setting Root Directory to `web` is what fixes the Python detection: Vercel only
inspects this folder, which contains no `requirements.txt`.

## Local check

    python -m http.server 4173 --directory web
    # open http://localhost:4173


---

## Two pages, two purposes

| Page | What it is | Needs the API? |
|---|---|---|
| `index.html` | Static case study. Build-time figures, zero config. | No |
| `command-center.html` | **Live product surface.** Reads the API at runtime. | Yes |

The case study stays deployable with no configuration at all, which is why the
zero-dependency decision above is unchanged. The Command Center is the live
surface and needs one thing: an API base URL.

### Pointing the Command Center at an API

Resolved at runtime, in this order:

1. `?api=https://…` in the query string
2. whatever was last entered in the header field (kept in `localStorage`)
3. `http://localhost:8000` when the page itself is served from localhost
4. otherwise unset — and the page says so, with the exact command to run

There is **no build-time environment variable**, deliberately. A static page with
a baked-in URL is a static page that breaks when the URL changes.

### Running it locally

```
uvicorn api.app.main:app --port 8000      # terminal 1
python -m http.server 4173 --directory web # terminal 2
```

Then open `http://localhost:4173/command-center.html`.

### Pointing it at a deployment

Paste the base URL into the header field, or load with `?api=`. The API must
allow the page's origin — see `ALLOWED_ORIGINS` in `api/app/main.py`, which
already lists the Vercel domain and `localhost:4173`.
