# AAP Configuration as Code - Quick Start

**Goal:** Populate your AAP 2.4 environment with test data for migration testing

**Time:** 5-10 minutes

---

## 🚀 Super Quick Start (One Command)

```bash
cd /Users/arbhati/project/git/aap-bridge-fork/aap-config-as-code

# Set your AAP token
export SOURCE__TOKEN="YOUR_SOURCE_AAP_TOKEN"

# Run interactive setup
./setup.sh
# Choose option 1 (Setup complete environment)
```

**That's it!** This will create 50+ resources in your AAP 2.4 environment.

---

## 📋 What Gets Created

| Resource Type | Count | Examples |
|---------------|-------|----------|
| **Organizations** | 3 | Engineering, Operations, QA |
| **Users** | 4+ | john_dev, jane_ops, bob_qa, alice_admin |
| **Teams** | 4 | Backend Developers, Infrastructure Team, QA Automation |
| **Credentials** | 12+ | SSH keys, Cloud (AWS/Azure), SCM, Vault, Network |
| **Projects** | 10 | Web App, Infrastructure, Database, Cloud, Security |
| **Inventories** | 5 | Dev Servers, Prod Servers, QA Servers, Cloud, Network |
| **Hosts** | 15+ | dev-web-01, prod-web-01, aws-ec2-web-01, etc. |
| **Job Templates** | 20+ | Deploy apps, configure DBs, security scans, backups |
| **Workflow Templates** | 5 | Full pipelines, DR workflows, compliance audits |

**Total:** 70+ resources ready for migration testing!

---

## 📖 Step-by-Step Guide

### Step 1: Install Ansible Collection (1 minute)

```bash
cd /Users/arbhati/project/git/aap-bridge-fork/aap-config-as-code

# Install required collection
ansible-galaxy collection install awx.awx --force
```

### Step 2: Set AAP Credentials (30 seconds)

```bash
# Use your existing token
export SOURCE__TOKEN="YOUR_SOURCE_AAP_TOKEN"

# Or set password if you prefer
export SOURCE_PASSWORD="your_admin_password"
```

### Step 3: Run Setup (5-8 minutes)

```bash
# Interactive mode
./setup.sh
```

**OR manual mode:**

```bash
# All at once (recommended)
ansible-playbook -i inventory.yml playbooks/00_setup_complete_environment.yml

# Or step by step
ansible-playbook -i inventory.yml playbooks/01_organizations_users.yml
ansible-playbook -i inventory.yml playbooks/02_credentials.yml
ansible-playbook -i inventory.yml playbooks/03_projects.yml
ansible-playbook -i inventory.yml playbooks/04_inventories.yml
ansible-playbook -i inventory.yml playbooks/05_job_templates.yml
ansible-playbook -i inventory.yml playbooks/06_workflow_templates.yml
```

### Step 4: Verify Setup (30 seconds)

```bash
ansible-playbook -i inventory.yml playbooks/99_verify_environment.yml
```

You should see:
```
╔═══════════════════════════════════════════════════════════╗
║  AAP 2.4 Environment Summary                              ║
╠═══════════════════════════════════════════════════════════╣
║  Organizations:        3                                  ║
║  Users:               7+                                  ║
║  Credentials:        12+                                  ║
║  Projects:           10                                   ║
║  Inventories:          5                                  ║
║  Hosts:              15+                                  ║
║  Job Templates:      20+                                  ║
║  Workflow Templates:   5                                  ║
╚═══════════════════════════════════════════════════════════╝
```

---

## 🧪 Test the Migration

Now that your AAP 2.4 is populated, test the migration:

```bash
# Go back to migration tool directory
cd /Users/arbhati/project/git/aap-bridge-fork

# Activate virtual environment
source .venv/bin/activate

# Fix credentials in target (if not done yet)
./scripts/fix_credentials_interactive.sh

# Run full migration
aap-bridge migrate full --config config/config.yaml

# Validate migration
aap-bridge validate all --sample-size 1000
```

---

## 🎯 What You Get for Testing

### Complex Organizational Structure
- 3 orgs with different purposes
- Users assigned to specific teams
- RBAC permissions

### Diverse Credentials
- **SSH**: Dev, Prod, QA keys
- **Cloud**: AWS, Azure credentials
- **SCM**: GitHub, GitLab access
- **Network**: Network device credentials
- **Vault**: Ansible Vault passwords

### Realistic Projects
- Git-based projects
- Various branches and repos
- Different update strategies

### Comprehensive Inventories
- Development environment (4 hosts)
- Production environment (6 hosts)
- QA environment (2 hosts)
- Cloud infrastructure (2 hosts)
- Network devices
- Groups and variables

### Production-Like Job Templates
- Application deployments
- Database management
- Security scanning
- Backup/restore procedures
- Cloud provisioning
- Container management
- CI/CD pipelines

### Complex Workflows
- Multi-stage deployment pipelines
- Disaster recovery procedures
- Compliance audit workflows
- Rolling updates with rollback

---

## 🔧 Customization

Want to create different data? Edit these files:

```bash
# Modify what gets created
nano playbooks/01_organizations_users.yml  # Change orgs, users, teams
nano playbooks/02_credentials.yml          # Add more credential types
nano playbooks/03_projects.yml             # Point to your git repos
nano playbooks/04_inventories.yml          # Add your actual hosts
nano playbooks/05_job_templates.yml        # Customize templates
nano playbooks/06_workflow_templates.yml   # Design workflows

# Then run again
./setup.sh
```

---

## 🧹 Clean Up (Optional)

If you want to start over:

```bash
# Delete all created resources (WARNING: destructive!)
# This playbook doesn't exist yet - create it if needed
# Or manually delete via AAP UI: Organizations → Delete
```

---

## 💡 Pro Tips

**Tip 1: Use Real Git Repos**
Edit `playbooks/03_projects.yml` to point to your actual repositories for more realistic testing.

**Tip 2: Add Your Hosts**
Edit `playbooks/04_inventories.yml` to add your actual infrastructure.

**Tip 3: Test Specific Scenarios**
Create job templates that match your actual use cases to test migration accuracy.

**Tip 4: Test Workflows**
Workflows are hard to migrate - create complex ones to test the tool's capabilities.

---

## ❓ Troubleshooting

### "awx.awx collection not found"
```bash
ansible-galaxy collection install awx.awx --force
```

### "Authentication failed"
```bash
# Make sure token is set
echo $SOURCE__TOKEN

# Or use password
export SOURCE_PASSWORD="your_password"
```

### "Organization already exists"
This is normal - Ansible will update existing resources. If you want fresh setup:
1. Delete organizations via AAP UI first
2. Or change names in playbooks

### "Project sync failed"
Some projects may fail to sync if git repos are unreachable. This is OK for testing - the structure will still be created.

---

## 📊 Expected Output

Running the complete setup should take **5-8 minutes** and show:

```
PLAY RECAP *********************************************************************
localhost : ok=50   changed=40   unreachable=0    failed=0    skipped=0

✓ AAP 2.4 environment setup complete!
Next steps:
  1. Verify data: ansible-playbook playbooks/99_verify_environment.yml
  2. Run migration: aap-bridge migrate full
  3. Validate migration: aap-bridge validate all
```

---

## 🎯 Success Criteria

After running setup, you should have:

- ✅ Multiple organizations with different structures
- ✅ Users assigned to teams with proper RBAC
- ✅ Diverse credential types (SSH, Cloud, SCM, Vault, Network)
- ✅ Projects synced from Git
- ✅ Inventories with groups and hosts
- ✅ Job templates ready to execute
- ✅ Workflow templates with complex logic

**This gives you a realistic AAP environment to thoroughly test migration!**

---

**Ready?** Run:

```bash
cd /Users/arbhati/project/git/aap-bridge-fork/aap-config-as-code
export SOURCE__TOKEN="YOUR_SOURCE_AAP_TOKEN"
./setup.sh
```

Choose option 1 and wait 5-8 minutes. Then test your migration!
