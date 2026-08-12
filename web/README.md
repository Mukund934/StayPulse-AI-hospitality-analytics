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
