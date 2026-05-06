#!/bin/bash

#
# AAP 2.4 Configuration as Code - Quickstart
#

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  AAP 2.4 Configuration as Code - Setup                    ║${NC}"
echo -e "${BLUE}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""

# Check if ansible-galaxy is installed
if ! command -v ansible-galaxy &> /dev/null; then
    echo -e "${YELLOW}⚠ Ansible not found. Please install Ansible first.${NC}"
    exit 1
fi

# Install required collections
echo -e "${BLUE}Installing required Ansible collections...${NC}"
ansible-galaxy collection install awx.awx --force

# Check if SOURCE__TOKEN is set
if [ -z "$SOURCE__TOKEN" ]; then
    echo -e "${YELLOW}⚠ SOURCE__TOKEN environment variable not set${NC}"
    echo "Please export your AAP token:"
    echo "  export SOURCE__TOKEN='your_token_here'"
    echo ""
    read -p "Or enter your AAP 2.4 admin password: " -s AAP_PASSWORD
    echo ""
    export SOURCE_PASSWORD="$AAP_PASSWORD"
fi

echo ""
echo -e "${BLUE}What would you like to do?${NC}"
echo ""
echo "1. Setup complete environment (all resources)"
echo "2. Setup organizations and users only"
echo "3. Setup credentials only"
echo "4. Setup projects only"
echo "5. Setup inventories only"
echo "6. Setup job templates only"
echo "7. Setup workflow templates only"
echo "8. Verify current environment"
echo "9. Exit"
echo ""
read -p "Select option (1-9): " option

case $option in
    1)
        echo -e "${GREEN}Setting up complete AAP environment...${NC}"
        ansible-playbook -i inventory.yml playbooks/00_setup_complete_environment.yml
        ;;
    2)
        echo -e "${GREEN}Setting up organizations and users...${NC}"
        ansible-playbook -i inventory.yml playbooks/01_organizations_users.yml
        ;;
    3)
        echo -e "${GREEN}Setting up credentials...${NC}"
        ansible-playbook -i inventory.yml playbooks/02_credentials.yml
        ;;
    4)
        echo -e "${GREEN}Setting up projects...${NC}"
        ansible-playbook -i inventory.yml playbooks/03_projects.yml
        ;;
    5)
        echo -e "${GREEN}Setting up inventories...${NC}"
        ansible-playbook -i inventory.yml playbooks/04_inventories.yml
        ;;
    6)
        echo -e "${GREEN}Setting up job templates...${NC}"
        ansible-playbook -i inventory.yml playbooks/05_job_templates.yml
        ;;
    7)
        echo -e "${GREEN}Setting up workflow templates...${NC}"
        ansible-playbook -i inventory.yml playbooks/06_workflow_templates.yml
        ;;
    8)
        echo -e "${GREEN}Verifying environment...${NC}"
        ansible-playbook -i inventory.yml playbooks/99_verify_environment.yml
        ;;
    9)
        echo "Exiting..."
        exit 0
        ;;
    *)
        echo -e "${YELLOW}Invalid option${NC}"
        exit 1
        ;;
esac

echo ""
echo -e "${GREEN}✓ Done!${NC}"
echo ""
echo "To verify what was created:"
echo "  ./setup.sh (choose option 8)"
echo ""
echo "To run migration:"
echo "  cd /Users/arbhati/project/git/aap-bridge-fork"
echo "  source .venv/bin/activate"
echo "  aap-bridge migrate full --config config/config.yaml"
echo ""
