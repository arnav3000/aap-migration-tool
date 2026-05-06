# External Applications & HashiCorp Vault Integration - Summary

## ✅ Successfully Created

Added OAuth2 applications for external monitoring system integrations and comprehensive HashiCorp Vault credential management with credential input sources.

---

## 📊 Quick Stats

| Resource Type | Count | Status |
|---------------|-------|--------|
| **OAuth2 Applications** | 5 | ✅ Created |
| **OAuth2 Tokens** | 2 | ✅ Created |
| **HashiCorp Vault Credentials** | 3 | ✅ Created |
| **Vault-Backed Credentials** | 2 | ✅ Created |
| **Credential Input Sources** | 4 | ✅ Created |
| **Total New Credentials** | 5 | (23 total in environment) |

---

## 🔌 OAUTH2 APPLICATIONS (External Monitoring Integrations)

### **1. Prometheus Monitoring**

| Property | Value |
|----------|-------|
| **Name** | Prometheus Monitoring |
| **Client Type** | Confidential |
| **Authorization Grant Type** | Authorization Code |
| **Redirect URI** | `https://prometheus.example.com/oauth/callback` |
| **Skip Authorization** | No (requires user consent) |
| **Organization** | Default |

**Purpose:**
- Query AAP metrics endpoint (`/api/v2/metrics/`)
- Monitor job success/failure rates
- Track resource usage and performance metrics
- Alert on automation failures

**OAuth2 Flow:**
1. Prometheus redirects user to AAP login
2. User authenticates and authorizes Prometheus
3. AAP redirects back to Prometheus with auth code
4. Prometheus exchanges code for access token
5. Uses token to query metrics API

---

### **2. Grafana Dashboard**

| Property | Value |
|----------|-------|
| **Name** | Grafana Dashboard |
| **Client Type** | Confidential |
| **Authorization Grant Type** | Authorization Code |
| **Redirect URIs** | `https://grafana.example.com/login/generic_oauth`<br>`https://grafana.example.com/oauth/callback` |
| **Skip Authorization** | No |
| **Organization** | Default |

**Purpose:**
- Create dashboards from AAP API data
- Visualize job execution trends over time
- Display inventory and host statistics
- Monitor infrastructure automation health

**Grafana Configuration:**
```ini
[auth.generic_oauth]
enabled = true
name = AAP OAuth
client_id = <client_id_from_aap>
client_secret = <client_secret_from_aap>
scopes = read
auth_url = https://localhost:8443/api/o/authorize/
token_url = https://localhost:8443/api/o/token/
api_url = https://localhost:8443/api/v2/me/
```

---

### **3. Splunk Log Aggregation**

| Property | Value |
|----------|-------|
| **Name** | Splunk Log Aggregation |
| **Client Type** | Confidential |
| **Authorization Grant Type** | Client Credentials (service-to-service) |
| **Redirect URIs** | (none - server-to-server) |
| **Skip Authorization** | Yes |
| **Organization** | Default |

**Purpose:**
- Collect AAP job logs via API
- Aggregate events from activity stream (`/api/v2/activity_stream/`)
- Search and analyze automation events
- Correlate AAP events with other system logs

**Client Credentials Flow:**
1. Splunk authenticates directly with client ID + secret
2. Receives access token without user interaction
3. Uses token to query logs and events
4. Periodic refresh for continuous collection

---

### **4. Datadog APM**

| Property | Value |
|----------|-------|
| **Name** | Datadog APM |
| **Client Type** | Confidential |
| **Authorization Grant Type** | Client Credentials |
| **Skip Authorization** | Yes |
| **Organization** | Default |

**Purpose:**
- Application performance monitoring for AAP
- Custom metrics from AAP API
- Trace job execution duration
- Alert on job failures or performance degradation

---

### **5. Custom Monitoring System**

| Property | Value |
|----------|-------|
| **Name** | Custom Monitoring System |
| **Client Type** | Public |
| **Authorization Grant Type** | Password (Resource Owner) |
| **Skip Authorization** | Yes |
| **Organization** | Default |

**Purpose:**
- Internal monitoring platform integration
- Direct username/password authentication
- Legacy system integration
- Custom monitoring tools

**Password Grant Flow:**
1. System sends username + password + client_id to token endpoint
2. AAP validates credentials
3. Returns access token
4. System uses token for API calls

---

## 🔐 HASHICORP VAULT CREDENTIALS

### **1. Production HashiCorp Vault** (IT Operations)

| Field | Value |
|-------|-------|
| **URL** | `https://vault.example.com:8200` |
| **Namespace** | `admin/aap` |
| **API Version** | `v2` (KV Secrets Engine v2) |
| **Auth Method** | **AppRole** |
| **Auth Path** | `approle` |
| **Role ID** | `$VAULT_ROLE_ID` |
| **Secret ID** | `$VAULT_SECRET_ID` |
| **CA Certificate** | Custom CA configured |

**AppRole Authentication:**
- Most secure method for AAP integration
- Role ID (non-sensitive) + Secret ID (sensitive)
- Secret ID can be wrapped for additional security
- Supports policies and TTL for secrets

**Use Cases:**
- Production environment secret management
- SSH credentials stored in Vault
- Database passwords managed by Vault
- AWS dynamic credentials from Vault

---

### **2. Development HashiCorp Vault** (Global Engineering)

| Field | Value |
|-------|-------|
| **URL** | `https://vault-dev.example.com:8200` |
| **Namespace** | `engineering/dev` |
| **API Version** | `v1` (KV Secrets Engine v1) |
| **Auth Method** | **Token** |
| **Token** | `hvs.DEVTokenForTestingPurposesOnly123456789` |

**Token Authentication:**
- Simple direct token authentication
- Suitable for development/testing
- Token has specific permissions via Vault policy
- Easier to configure but less secure than AppRole

**Use Cases:**
- Development environment testing
- Non-production workloads
- Temporary integrations

---

### **3. Kubernetes HashiCorp Vault** (Cloud Services)

| Field | Value |
|-------|-------|
| **URL** | `https://vault-k8s.example.com:8200` |
| **Namespace** | `cloud/kubernetes` |
| **API Version** | `v2` |
| **Auth Method** | **Kubernetes** |
| **Auth Path** | `kubernetes` |
| **Kubernetes Role** | `aap-automation-role` |

**Kubernetes Authentication:**
- Uses Kubernetes service account JWT
- Seamless integration when AAP runs in K8s
- No credentials to manage (uses pod identity)
- Automatically rotates tokens

**Use Cases:**
- AAP running in Kubernetes/OpenShift
- Cloud-native deployments
- Container-based workloads

---

## 🔗 VAULT-BACKED CREDENTIALS (Credential Input Sources)

### **1. Vault-Backed SSH Credential**

| Property | Value |
|----------|-------|
| **Name** | Vault-Backed SSH Credential |
| **Type** | Machine (SSH) |
| **Organization** | IT Operations |
| **Username** | `ansible-automation` (static) |
| **Vault Source** | Production HashiCorp Vault |

**Credential Input Sources:**

| AAP Field | Vault Path | Vault Key | Description |
|-----------|------------|-----------|-------------|
| `password` | `secret/data/ssh/production` | `password` | SSH password fetched from Vault |
| `ssh_key_data` | `secret/data/ssh/production` | `private_key` | SSH private key fetched from Vault |

**How It Works:**
1. Job template uses this credential
2. At job runtime, AAP contacts Vault
3. Authenticates using AppRole (role_id + secret_id)
4. Fetches password from `secret/data/ssh/production → password`
5. Fetches SSH key from `secret/data/ssh/production → private_key`
6. Injects both into the job environment
7. Job runs with actual secrets from Vault

**Benefits:**
- Secrets never stored in AAP database
- Centralized secret management
- Audit trail in Vault
- Secret rotation without updating AAP

---

### **2. Vault-Backed AWS Credential**

| Property | Value |
|----------|-------|
| **Name** | Vault-Backed AWS Credential |
| **Type** | Amazon Web Services |
| **Organization** | Cloud Services |
| **Vault Source** | Production HashiCorp Vault |

**Credential Input Sources:**

| AAP Field | Vault Path | Vault Key | Description |
|-----------|------------|-----------|-------------|
| `username` (Access Key) | `aws/creds/aap-automation-role` | `access_key` | Dynamic AWS access key |
| `password` (Secret Key) | `aws/creds/aap-automation-role` | `secret_key` | Dynamic AWS secret key |

**Dynamic Credentials:**
This uses Vault's AWS Secrets Engine to generate credentials on-demand:

1. Job template uses this credential
2. At job runtime, AAP requests credentials from Vault
3. Vault generates NEW AWS credentials via AWS IAM
4. Returns temporary access_key + secret_key
5. AAP injects into job
6. Job runs with fresh AWS credentials
7. Vault automatically revokes credentials after TTL

**Benefits:**
- Fresh credentials for every job
- No long-lived AWS keys in AAP
- Automatic credential rotation
- Minimal blast radius if compromised
- Full audit trail of credential usage

---

## 🔄 CREDENTIAL INPUT SOURCE FLOW

```
┌─────────────────┐
│  Job Template   │
│  starts running │
└────────┬────────┘
         │
         ├─ Uses "Vault-Backed SSH Credential"
         │
         v
┌─────────────────────────────────────────────────────┐
│  AAP Controller                                     │
│                                                     │
│  1. Identifies credential has input sources        │
│  2. Finds source: "Production HashiCorp Vault"     │
│  3. Authenticates to Vault using AppRole:          │
│     - role_id: 12345678-1234-1234-1234-...        │
│     - secret_id: 87654321-4321-4321-4321-...      │
└────────┬────────────────────────────────────────────┘
         │
         v
┌─────────────────────────────────────────────────────┐
│  HashiCorp Vault                                    │
│  URL: https://vault.example.com:8200                │
│  Namespace: admin/aap                               │
│                                                     │
│  1. Validates AppRole credentials                  │
│  2. Checks policy permissions                      │
│  3. Fetches secrets:                               │
│     - secret/data/ssh/production → password        │
│     - secret/data/ssh/production → private_key     │
│  4. Returns secrets to AAP                         │
└────────┬────────────────────────────────────────────┘
         │
         v
┌─────────────────────────────────────────────────────┐
│  AAP Job Execution                                  │
│                                                     │
│  Environment Variables:                             │
│  - ansible_user: ansible-automation (static)       │
│  - ansible_password: <from Vault>                  │
│  - ansible_ssh_private_key_file: <from Vault>      │
│                                                     │
│  Job runs with actual secrets ✓                    │
└─────────────────────────────────────────────────────┘
```

---

## 📋 OAUTH2 GRANT TYPES EXPLAINED

### **Authorization Code (Prometheus, Grafana)**
- **Flow:** User interactive
- **Use Case:** Web applications accessing API on behalf of users
- **Security:** Most secure for user-facing apps
- **Steps:**
  1. App redirects user to AAP login
  2. User logs in and authorizes
  3. AAP redirects back with code
  4. App exchanges code for token
  5. App uses token to access API

### **Client Credentials (Splunk, Datadog)**
- **Flow:** Server-to-server
- **Use Case:** Background services, daemons, monitoring
- **Security:** No user interaction needed
- **Steps:**
  1. Service authenticates with client_id + client_secret
  2. Receives access token immediately
  3. Uses token for API calls
  4. Refreshes token as needed

### **Password/Resource Owner (Custom Monitoring)**
- **Flow:** Direct username/password
- **Use Case:** Legacy systems, trusted applications
- **Security:** Less secure (exposes user credentials)
- **Steps:**
  1. App sends username + password + client_id
  2. AAP validates credentials
  3. Returns access token
  4. App uses token for API calls

---

## 🧪 MIGRATION TESTING VALUE

### **OAuth2 Applications Migration:**

✅ **Application Definitions:**
- Application names and descriptions
- Client types (confidential, public)
- Authorization grant types
- Organization associations

✅ **OAuth2 Configuration:**
- Client IDs (non-sensitive)
- Client secrets (encrypted)
- Redirect URIs (multiple per app)
- Skip authorization flags

✅ **Token Management:**
- OAuth2 access tokens
- Token scopes (read, write)
- Token expiration
- Refresh token handling

---

### **HashiCorp Vault Integration Migration:**

✅ **Vault Credential Types:**
- Different auth methods (AppRole, Token, Kubernetes)
- Vault server URLs and namespaces
- API versions (v1, v2)
- Custom CA certificates

✅ **Credential Input Sources:**
- Source credential references
- Input field name mappings
- Vault secret paths
- Vault secret keys
- Secret version specifications (v2)

✅ **Dynamic Secret Fetching:**
- Runtime secret retrieval
- Multiple input sources per credential
- Different Vault backends (KV, AWS)
- Path and key configurations

---

## 📊 COMPLETE AAP 2.4 ENVIRONMENT - FINAL STATE

```
╔══════════════════════════════════════════════════════════════════╗
║   AAP 2.4 - ENTERPRISE-GRADE COMPREHENSIVE TEST ENVIRONMENT       ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                   ║
║  🔐 AUTHENTICATION & AUTHORIZATION:                               ║
║    • LDAP Servers:                2  (Primary + Backup)           ║
║    • LDAP Org Mappings:           3  (auto-provisioning)          ║
║    • LDAP Team Mappings:          3  (auto team assignment)       ║
║    • Organizations:               9  (with EE defaults)           ║
║    • Users:                      23  (+ LDAP SSO)                 ║
║    • Teams:                      14  (cross-org)                  ║
║                                                                   ║
║  🔌 EXTERNAL INTEGRATIONS:                                        ║
║    • OAuth2 Applications:         5  (monitoring systems)         ║
║    • OAuth2 Tokens:               2  (API access)                 ║
║                                                                   ║
║  🔑 CREDENTIALS & SECRETS:                                        ║
║    • Standard Credentials:       13  (SSH, Git, Cloud)            ║
║    • Custom Type Credentials:     5  (custom injectors)           ║
║    • HashiCorp Vault Creds:       3  (3 auth methods)             ║
║    • Vault-Backed Credentials:    2  (dynamic secrets)            ║
║    • Credential Input Sources:    4  (Vault integration)          ║
║    • Custom Credential Types:     6  (complex types)              ║
║    • Total Credentials:          23                               ║
║                                                                   ║
║  📦 PROJECTS & CODE:                                              ║
║    • Projects:                    7  (Git-based)                  ║
║    • Project Update Schedules:    5  (auto-sync)                  ║
║                                                                   ║
║  📋 INVENTORIES & HOSTS:                                          ║
║    • Inventories:                10  (static + SCM)               ║
║    • Inventory Sources:           3  (Git-based)                  ║
║    • Inventory Schedules:         2  (auto-sync)                  ║
║    • Hosts:                      21  (rich variables)             ║
║    • Groups:                      9  (nested)                     ║
║                                                                   ║
║  🎯 AUTOMATION & JOBS:                                            ║
║    • Job Templates:               7  (interconnected)             ║
║    • Job Schedules:               9  (various frequencies)        ║
║    • Workflow Templates:          0                               ║
║                                                                   ║
║  🐳 EXECUTION INFRASTRUCTURE:                                     ║
║    • Execution Environments:     12  (specialized)                ║
║    • Instance Groups:             3  (Prod, Dev, QA)              ║
║    • Execution Instances:         4  (3 exec + 1 hop)             ║
║                                                                   ║
║  ⏰ SCHEDULES:                                                     ║
║    • Total Schedules:            16  (jobs + projects + invs)     ║
║                                                                   ║
║  ⚙️ SYSTEM SETTINGS:                                              ║
║    • LDAP Integration:        Configured                          ║
║    • System Settings:         Configured                          ║
║    • UI Customization:        Configured                          ║
║    • Job Settings:            Configured                          ║
║                                                                   ║
║  📊 TOTAL CONFIGURATION OBJECTS: 190+                             ║
║                                                                   ║
╚══════════════════════════════════════════════════════════════════╝
```

---

## 🔍 Verification Commands

### **View OAuth2 Applications:**

```bash
# List all OAuth2 applications
curl -sk -H "Authorization: Bearer $SOURCE__TOKEN" \
  https://localhost:8443/api/v2/applications/ | jq

# Get specific application details
curl -sk -H "Authorization: Bearer $SOURCE__TOKEN" \
  https://localhost:8443/api/v2/applications/1/ | jq

# View application client_id and client_secret
curl -sk -H "Authorization: Bearer $SOURCE__TOKEN" \
  https://localhost:8443/api/v2/applications/1/ | \
  jq '{client_id, client_secret, authorization_grant_type}'
```

### **View HashiCorp Vault Credentials:**

```bash
# List Vault credentials
curl -sk -H "Authorization: Bearer $SOURCE__TOKEN" \
  https://localhost:8443/api/v2/credentials/?credential_type__kind=vault | jq

# Get specific Vault credential
curl -sk -H "Authorization: Bearer $SOURCE__TOKEN" \
  https://localhost:8443/api/v2/credentials/?name=Production%20HashiCorp%20Vault | jq
```

### **View Credential Input Sources:**

```bash
# Get credential ID
CRED_ID=$(curl -sk -H "Authorization: Bearer $SOURCE__TOKEN" \
  https://localhost:8443/api/v2/credentials/?name=Vault-Backed%20SSH%20Credential | \
  jq -r '.results[0].id')

# View input sources for credential
curl -sk -H "Authorization: Bearer $SOURCE__TOKEN" \
  https://localhost:8443/api/v2/credentials/$CRED_ID/input_sources/ | jq

# View detailed input source configuration
curl -sk -H "Authorization: Bearer $SOURCE__TOKEN" \
  https://localhost:8443/api/v2/credentials/$CRED_ID/input_sources/1/ | jq
```

---

## 🚀 READY FOR COMPREHENSIVE MIGRATION!

Your AAP 2.4 environment now includes:

### ✅ **All Core AAP Resources (150+ objects)**
### ✅ **Advanced Features (40+ configurations)**
### ✅ **External Integrations (10+ integrations)**
### ✅ **System Settings (50+ settings)**

**Total:** 190+ configuration objects representing an enterprise-grade AAP deployment

---

## 📝 Migration Command

```bash
cd /Users/arbhati/project/git/aap-bridge-fork
source .venv/bin/activate

# Run comprehensive migration
aap-bridge migrate full --config config/config.yaml

# Validate everything migrated correctly
aap-bridge validate all --sample-size 1000
```

**Expected Migration Coverage:**
- ✅ 9 Organizations with configs
- ✅ 23 Users with RBAC
- ✅ 14 Teams with members
- ✅ 23 Credentials (including Vault)
- ✅ 6 Custom credential types
- ✅ 4 Credential input sources
- ✅ 5 OAuth2 applications
- ✅ 7 Projects with schedules
- ✅ 10 Inventories (static + SCM)
- ✅ 21 Hosts with variables
- ✅ 7 Job templates
- ✅ 16 Schedules
- ✅ 12 Execution environments
- ✅ LDAP settings
- ✅ System settings
- ✅ All relationships and dependencies

**Total Objects:** 190+ configuration items

Your AAP 2.4 environment is **enterprise-ready** for the most comprehensive migration testing possible! 🎉
