# Security Overview

DocMind operates a multi-tenant retrieval service and treats customer corpora
as confidential data. This overview summarizes the controls that are verified
every year by independent auditors.

## Certifications

DocMind holds a SOC 2 Type II report covering security and availability. The
report is renewed annually and is available to customers under a non-disclosure
agreement. An ISO 27001 certification is planned but is not yet complete.

## Encryption

Data at rest is encrypted with AES-256 encryption on managed disks and object
storage. Data in transit is protected with TLS 1.3 for every public endpoint;
older protocol versions are refused at the load balancer. Embedding vectors are
stored in the same encrypted database as the source chunks.

## Access control

Administrators must enable multi-factor authentication. Multi-factor
authentication is enforced for every user who can read customer documents.
Role-based access control separates the billing, support, and engineering
roles. Production access requires short-lived credentials and is reviewed every
quarter.

## Testing and monitoring

External penetration tests are performed annually by an independent firm, and a
remediation plan is agreed for every finding. Vulnerability scanning runs on
every container image build. Audit logs are retained for 400 days and are
streamed to the customer on request.

## Incident response

Security incidents are triaged within one hour. Customers affected by an
incident are notified within 72 hours, matching the contractual notification
window. A public status page reports outages without disclosing customer data.
