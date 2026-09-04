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

resource "coder_agent" "main" {
  arch = data.coder_provisioner.me.arch
  os   = "linux"
  dir  = "/home/coder"

  startup_script = <<-EOT
    set -eu
    if [ ! -f ~/.harness-base-ready ]; then
      sudo apt-get update
      sudo apt-get install -y --no-install-recommends git curl ca-certificates jq python3 python3-venv nodejs npm
      touch ~/.harness-base-ready
    fi
    mkdir -p /home/coder/task
    if [ -n "${data.coder_parameter.repo_url.value}" ] && [ ! -d /home/coder/task/.git ]; then
      git clone --branch "${data.coder_parameter.base_ref.value}" --single-branch "${data.coder_parameter.repo_url.value}" /home/coder/task
    fi
  EOT

  env = {
    GIT_AUTHOR_NAME    = coalesce(data.coder_workspace_owner.me.full_name, data.coder_workspace_owner.me.name)
    GIT_AUTHOR_EMAIL   = data.coder_workspace_owner.me.email
    GIT_COMMITTER_NAME = coalesce(data.coder_workspace_owner.me.full_name, data.coder_workspace_owner.me.name)
    GIT_COMMITTER_EMAIL = data.coder_workspace_owner.me.email
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
