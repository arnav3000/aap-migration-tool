# AAP 2.4 Configuration as Code

This directory contains Ansible playbooks to populate your AAP 2.4 environment with test data for migration testing.

## Overview

These playbooks will create:
- **3 Organizations** with different structures
- **10+ Projects** (git-based, manual, demo)
- **15+ Credentials** (SSH, Vault, Cloud, SCM, Galaxy)
- **5 Inventories** with hosts and groups
- **20+ Job Templates** with various configurations
- **5 Workflow Templates** with complex dependencies
- **Users and Teams** with RBAC assignments
- **Schedules and Notifications**

## Prerequisites

```bash
# Install ansible.controller collection
ansible-galaxy collection install ansible.controller

# Or for AAP 2.4, you might need awx.awx
ansible-galaxy collection install awx.awx
```

## Configuration

Edit `vars/aap_config.yml` with your AAP 2.4 details:

```yaml
controller_hostname: localhost:8443
controller_username: admin
controller_password: your_password
controller_validate_certs: false
```

## Usage

### Option 1: Run All (Complete Setup)

```bash
# Populate entire AAP environment
ansible-playbook -i inventory.yml playbooks/00_setup_complete_environment.yml
```

### Option 2: Run Step-by-Step

```bash
# 1. Organizations and Users
ansible-playbook -i inventory.yml playbooks/01_organizations_users.yml

# 2. Credentials
ansible-playbook -i inventory.yml playbooks/02_credentials.yml

# 3. Projects
ansible-playbook -i inventory.yml playbooks/03_projects.yml

# 4. Inventories
ansible-playbook -i inventory.yml playbooks/04_inventories.yml

# 5. Job Templates
ansible-playbook -i inventory.yml playbooks/05_job_templates.yml

# 6. Workflow Templates
ansible-playbook -i inventory.yml playbooks/06_workflow_templates.yml
```

### Option 3: Run Individual Components

```bash
# Just create credentials
ansible-playbook -i inventory.yml playbooks/02_credentials.yml --tags credentials

# Just create SSH credentials
ansible-playbook -i inventory.yml playbooks/02_credentials.yml --tags ssh_credentials
```

## What Gets Created

### Organizations
- **Engineering Org** - Development team structure
- **Operations Org** - Infrastructure team structure
- **QA Org** - Testing team structure

### Projects
- Demo Project (built-in)
- Infrastructure as Code (git)
- Web Application Deployment (git)
- Database Management (git)
- Network Automation (git)
- Security Compliance (git)
- Cloud Provisioning (git)

### Credentials
- SSH Keys (3 types: dev, prod, admin)
- Cloud Providers (AWS, Azure, GCP)
- Source Control (GitHub, GitLab)
- Vault Secrets
- Container Registries
- Galaxy/Automation Hub

### Inventories
- Development Servers
- Production Servers
- Cloud Infrastructure
- Network Devices
- Container Hosts

### Job Templates
- Simple ping tests
- Package installation
- Configuration management
- Application deployment
- Database backup/restore
- Security scanning
- Cloud resource provisioning

### Workflow Templates
- Full application deployment pipeline
- Disaster recovery workflow
- Compliance audit workflow
- Multi-cloud deployment
- Rolling update workflow

## Testing the Migration

After populating AAP 2.4:

```bash
# 1. Verify data created
ansible-playbook -i inventory.yml playbooks/99_verify_environment.yml

# 2. Run migration to AAP 2.6
cd /Users/arbhati/project/git/aap-bridge-fork
source .venv/bin/activate
aap-bridge migrate full --config config/config.yaml

# 3. Validate migration results
aap-bridge validate all --sample-size 1000
```

## Cleanup

To remove all created data:

```bash
ansible-playbook -i inventory.yml playbooks/98_cleanup_environment.yml
```

## Customization

Edit the vars files to customize what gets created:

- `vars/organizations.yml` - Organization definitions
- `vars/credentials.yml` - Credential definitions
- `vars/projects.yml` - Project definitions
- `vars/inventories.yml` - Inventory definitions
- `vars/job_templates.yml` - Job template definitions
- `vars/workflows.yml` - Workflow definitions

## Notes

- All credentials use placeholder values (you'll need real secrets for actual use)
- Projects point to public git repos (change to your repos if needed)
- Inventories use example hosts (update with your actual infrastructure)
- This is designed for **testing migration**, not production use
