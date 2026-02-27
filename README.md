# CloudLaunch

A cloud infrastructure automation platform built in Python. Provisions a real Azure VM, deploys a containerised web application over SSH, and streams every operation live via WebSocket — all through a REST API, a CLI, and a single-file dashboard.

[![Live Demo](https://img.shields.io/badge/Live_Demo-Try_It-2ea043?style=flat-square&logo=azure-devops)](http://vmlaunch.404by.me/)
[![Python](https://img.shields.io/badge/Python-3.8-3776ab?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com)
[![Azure](https://img.shields.io/badge/Azure-SDK-0078d4?style=flat-square&logo=microsoft-azure)](https://learn.microsoft.com/en-us/azure/developer/python/)

---

## Live Demo

**[vmlaunch.404by.me](http://vmlaunch.404by.me/)** — fully live on a real Azure subscription.

Open the dashboard, click **Provision VM**, watch the infrastructure build in ~90 seconds, deploy a Docker app, visit the running application, then destroy everything. Every log line streams in real time. The VM auto-destroys after 20 minutes.

---

## What it does

| Operation | What happens |
|---|---|
| **Provision** | Creates a Resource Group, VNet, NSG, Public IP, NIC, and Ubuntu 22.04 VM via the Azure SDK |
| **Deploy** | SSHes into the VM (Paramiko), installs Docker, uploads the app via SFTP, builds and runs the Nginx container |
| **Status** | Calls the Azure Compute API for live VM power state, IP, size, and disk info |
| **Destroy** | Deletes the entire resource group in one SDK call — all 6 resources gone |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Client Layer                                               │
│  Browser (dashboard.html) · CLI (Typer) · REST (curl)       │
└──────────────────────┬──────────────────────────────────────┘
                       │ HTTP / WebSocket
┌──────────────────────▼──────────────────────────────────────┐
│  FastAPI + Uvicorn                                          │
│  POST /provision  /deploy  /destroy  /status                │
│  WS   /ws/{job_id}                                          │
│  Middleware: rate limiter · concurrency cap · VM budget      │
└──────┬──────────────────────────────┬───────────────────────┘
       │ launch_job()                 │ run_in_executor()
┌──────▼──────────┐        ┌──────────▼──────────────────────┐
│ Background      │        │ WebSocket Handler               │
│ Thread          │◄──────►│ Async queue drain               │
│ (sync SDK code) │queue   │ Heartbeat · log replay          │
└──────┬──────────┘        └─────────────────────────────────┘
       │ Azure SDK (blocking)
┌──────▼──────────────────────────────────────────────────────┐
│  Azure ARM APIs                                             │
│  ResourceManagementClient · NetworkManagementClient          │
│  ComputeManagementClient                                    │
└──────┬──────────────────────────────────────────────────────┘
       │ VM ready → Paramiko SSH
┌──────▼──────────────────────────────────────────────────────┐
│  Ubuntu 22.04 VM  (Standard_B1s · Southeast Asia)           │
│  Docker → Nginx container (port 80)                         │
└─────────────────────────────────────────────────────────────┘
```

State is persisted to `state.json` (IP, resource group, SSH key path). No database required at this scale.

---

## Tech Stack

| Layer | Choice | Why |
|---|---|---|
| API | FastAPI + Uvicorn | Async-native, first-class WebSocket, Pydantic v2 validation |
| Cloud | Azure SDK for Python | Direct ARM calls — no Terraform, full control over error handling |
| SSH / SFTP | Paramiko 3.x | Pure-Python SSH2, handles key auth + SFTP without subprocesses |
| Concurrency | Daemon threads + `run_in_executor` | Sync SDK runs in threads; async event loop never blocked |
| CLI | Typer + Rich | Type-annotated commands, Rich tables and progress output |
| Frontend | Vanilla JS | No build step, served as a static file, WebSocket + Fetch API |
| Auth (Azure) | Service Principal | `DefaultAzureCredential`, credentials in env vars only |

---

## Abuse Protection

The demo is publicly accessible with no API key. Four independent guards prevent runaway spend:

1. **Token-bucket rate limiter** — per IP, 4 write ops/min, 60 read ops/min (ASGI middleware)
2. **Global concurrency cap** — 1 active job at a time, tracked with a threading lock
3. **Active VM budget** — maximum 3 provisioned VMs; counter decrements on destroy
4. **Mandatory auto-destroy** — server-side `threading.Timer` fires after 20 minutes; cannot be cancelled by the browser

---

## Project Structure

```
cloud-cli-tool/
├── api/
│   ├── app.py           # FastAPI routes, WebSocket handler, startup recovery
│   ├── jobs.py          # Job store, background thread runner, queue management
│   ├── middleware.py     # Rate limiter, concurrency cap, VM budget, auto-destroy TTL
│   └── schemas.py       # Pydantic request/response models
├── providers/
│   ├── base.py          # Abstract BaseProvider interface
│   └── azure_provider.py # Azure SDK implementation (provision/deploy/destroy/status)
├── cli/
│   └── display.py       # Rich-based terminal output for the CLI
├── static/
│   └── dashboard.html   # Single-file web dashboard (Vanilla JS, WebSocket, Fetch)
├── sample_app/
│   └── index.template.html  # Template rendered and deployed to the VM
├── main.py              # Typer CLI entry point
├── server.py            # Uvicorn server launcher
├── Dockerfile           # Container image for the server itself
└── requirements.txt
```

---

## Running Locally

### Prerequisites

- Python 3.8+
- An Azure subscription with a Service Principal ([create one](https://learn.microsoft.com/en-us/cli/azure/create-an-azure-service-principal-azure-cli))
- An SSH key pair at `~/.ssh/id_rsa` / `~/.ssh/id_rsa.pub`

### Setup

```bash
git clone https://github.com/unknown788/cloud-cli-tool.git
cd cloud-cli-tool

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Configure credentials

Copy `.env.example` to `.env` and fill in your Azure Service Principal values:

```bash
cp .env.example .env
```

```env
AZURE_SUBSCRIPTION_ID=your-subscription-id
AZURE_CLIENT_ID=your-client-id
AZURE_CLIENT_SECRET=your-client-secret
AZURE_TENANT_ID=your-tenant-id
```

### Start the API server

```bash
set -a && . ./.env && set +a
python3 -m uvicorn api.app:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000** for the dashboard, or **http://localhost:8000/docs** for the OpenAPI UI.

### CLI usage

```bash
# Provision a VM
python3 main.py provision

# Deploy the app (after provision)
python3 main.py deploy

# Check status
python3 main.py status

# Tear down all resources
python3 main.py destroy
```

---

## Running with Docker

```bash
docker build -t cloudlaunch .
docker run -p 8000:8000 --env-file .env cloudlaunch
```

---

## API Reference

| Method | Path | Description |
|---|---|---|
| `POST` | `/provision` | Create Azure VM stack, returns `job_id` |
| `POST` | `/deploy` | Deploy Docker app to provisioned VM, returns `job_id` |
| `POST` | `/destroy` | Delete all Azure resources, returns `job_id` |
| `GET` | `/status` | Live VM power state, IP, size |
| `GET` | `/state` | Current provisioned VM metadata (404 if none) |
| `GET` | `/jobs` | All jobs with status and logs |
| `GET` | `/jobs/{job_id}` | Single job detail |
| `WS` | `/ws/{job_id}` | Real-time log stream for a job |
| `GET` | `/quota` | Active VM count, limits, and TTL remaining |
| `GET` | `/plan` | Preview of resources that will be created |

---

## Design Notes

**Why threads instead of async for provider code?**
The Azure SDK and Paramiko are synchronous. Running them in daemon threads keeps the model explicit — one thread per job. The WebSocket handler bridges back to async via `run_in_executor()`, so the event loop is never blocked.

**Why a flat file instead of a database?**
One write (provision), one delete (destroy), no concurrent writers. A database adds a service dependency with no benefit at this access pattern. The file survives server restarts; the startup handler reads it and restores in-memory state.

**Why a provider abstraction?**
`BaseProvider` defines `provision()`, `deploy()`, `destroy()`, `get_status()`. `AzureProvider` implements it. Adding AWS or GCP means one new file — the API, CLI, and dashboard have zero changes.

---

## License

MIT
