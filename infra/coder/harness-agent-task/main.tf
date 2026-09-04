terraform {
  required_providers {
    coder = {
      source = "coder/coder"
    }
    docker = {
      source = "kreuzwerker/docker"
    }
  }
}

variable "docker_socket" {
  type        = string
  default     = ""
  description = "Optional Docker socket URI for the selected Coder server."
}

provider "docker" {
  host = var.docker_socket != "" ? var.docker_socket : null
}

data "coder_provisioner" "me" {}
data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

data "coder_parameter" "repo_url" {
  name         = "repo_url"
  display_name = "Repository URL"
  description  = "Git repository checked out into /home/coder/task. Leave empty for an empty task workspace."
  type         = "string"
  default      = ""
  mutable      = false
}

data "coder_parameter" "base_ref" {
  name         = "base_ref"
  display_name = "Base branch or ref"
  description  = "Git ref to check out when creating the workspace."
  type         = "string"
  default      = "main"
  mutable      = false
}

data "coder_parameter" "auth_provider_id" {
  name         = "auth_provider_id"
  display_name = "Repository connection"
  description  = "Coder external-auth provider ID used for this repository."
  type         = "string"
  default      = "github"
  mutable      = false
}

data "coder_external_auth" "repository" {
  id = data.coder_parameter.auth_provider_id.value
}

resource "coder_agent" "main" {
  arch = data.coder_provisioner.me.arch
  os   = "linux"

  startup_script = <<-EOT
    set -eu
    export GIT_TERMINAL_PROMPT=0
    if [ ! -f ~/.harness-base-ready ]; then
      sudo apt-get update
      sudo apt-get install -y --no-install-recommends git curl ca-certificates jq python3 python3-venv nodejs npm
      touch ~/.harness-base-ready
    fi
    if [ ! -x ~/.codex/packages/standalone/current/bin/codex ]; then
      curl -fsSL https://chatgpt.com/codex/install.sh | sh
    fi
    mkdir -p /home/coder/task
    if [ -n "$HARNESS_REPO_URL" ] && [ ! -d /home/coder/task/.git ]; then
      git clone --branch "$HARNESS_BASE_REF" --single-branch -- "$HARNESS_REPO_URL" /home/coder/task
    fi
  EOT

  env = {
    HARNESS_REPO_URL   = data.coder_parameter.repo_url.value
    HARNESS_BASE_REF   = data.coder_parameter.base_ref.value
  }

  metadata {
    display_name = "CPU Usage"
    key          = "cpu_usage"
    script       = "coder stat cpu"
    interval     = 10
    timeout      = 1
  }
  metadata {
    display_name = "Task Disk"
    key          = "task_disk"
    script       = "coder stat disk --path /home/coder/task"
    interval     = 60
    timeout      = 1
  }
}

resource "docker_volume" "home" {
  name = "harness-${data.coder_workspace.me.id}-home"
  lifecycle {
    ignore_changes = all
  }
  labels {
    label = "harness.workspace_id"
    value = data.coder_workspace.me.id
  }
}

resource "docker_container" "workspace" {
  count      = data.coder_workspace.me.start_count
  image      = "codercom/enterprise-base:ubuntu"
  name       = "harness-${data.coder_workspace_owner.me.name}-${lower(data.coder_workspace.me.name)}"
  hostname   = data.coder_workspace.me.name
  entrypoint = ["sh", "-c", replace(coder_agent.main.init_script, "/localhost|127\\.0\\.0\\.1/", "host.docker.internal")]
  env        = ["CODER_AGENT_TOKEN=${coder_agent.main.token}"]

  host {
    host = "host.docker.internal"
    ip   = "host-gateway"
  }
  volumes {
    container_path = "/home/coder"
    volume_name    = docker_volume.home.name
    read_only      = false
  }
  labels {
    label = "harness.workspace_id"
    value = data.coder_workspace.me.id
  }
}
