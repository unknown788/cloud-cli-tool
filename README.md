# Cloud Provisioning & Deployment CLI 🚀

A Python-based command-line tool that automates the entire lifecycle of cloud infrastructure on Azure. This tool provisions a complete VM stack from scratch, deploys a containerized web application, and tears down all resources with a single set of commands.

---

## ✨ Live Demo

This recording shows the tool's full workflow:
1.  `provision`: Building the entire cloud infrastructure.
2.  `deploy`: Deploying the dynamic web application using Docker.
3.  `destroy`: Tearing down all cloud resources.

[![Live Demo](https://img.shields.io/badge/Live_Demo-vmlaunch.404by.me-blue?style=for-the-badge&logo=vercel)](http://vmlaunch.404by.me/)

---

##  Workflow Architecture

This tool automates a 3-step process, all managed from a single CLI.

[Image of a workflow diagram showing a developer running the CLI. 
Step 1 'provision' points to an Azure cloud icon containing a VM, VNet, and NSG. 
Step 2 'deploy' shows Docker and a code icon pointing to the VM. 
Step 3 'destroy' shows the Azure resources being deleted.]

1.  **Provision:** The `provision` command communicates with the Azure Resource Manager (ARM) API using the Azure SDK to build all necessary resources (VM, VNet, Public IP, NSG) based on the Python script's logic.
2.  **Deploy:** The `deploy` command:
    * Generates a dynamic `index.html` dashboard.
    * Connects to the VM via SSH (Paramiko).
    * Installs Docker on the remote VM.
    * Uploads the `Dockerfile` and the generated web application.
    * Builds a new Nginx Docker image on the VM.
    * Runs the container, mapping port 80 to serve the web dashboard.
3.  **Destroy:** The `destroy` command calls the Azure CLI to delete the entire resource group, removing all created resources and stopping all costs.

---

## Core Features

* **Infrastructure as Code (IaC):** Manages all cloud resources programmatically, ensuring repeatable and consistent environments.
* **Automated Provisioning:** Creates a complete, secure cloud environment (VM, VNet, Public IP, Firewall) with one command.
* **Containerized Deployment:** Deploys a dynamic web application dashboard using **Docker** and **Nginx**, showcasing a modern, container-based workflow.
* **Full Lifecycle Management:** Includes a `destroy` command to tear down all resources, demonstrating full control over the infrastructure lifecycle.
* **State Management:** Uses a local `state.json` file to track the provisioned infrastructure's IP address and metadata.

---

## 🛠️ Technologies Used

* **Core:** Python, Typer (for the CLI)
* **Cloud:** Microsoft Azure SDK for Python (azure-mgmt-compute, azure-mgmt-network)
* **Containerization:** Docker & Nginx
* **Remote Execution:** Paramiko (SSH)
* **System:** Linux/Bash, Git

---

## ⚙️ Setup & Configuration

### 1. Prerequisites
* [Python 3.8+](https://www.python.org/)
* [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli)
* An active Azure Subscription (e.g., Azure for Students)

### 2. Clone & Install
```bash
# Clone the repository
git clone https://github.com/unknown788/cloud-cli-tool.git
cd cloud-cli-tool

# Create a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt




