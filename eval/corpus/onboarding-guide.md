# Onboarding Guide

Onboarding is a structured 14-day onboarding program that takes a new customer
from contract signature to the first production query. The program is led by a
customer success engineer and is included in the Team and Enterprise plans.

## Kickoff

The kickoff call is scheduled within two business days of contract signature.
During the call the team reviews the use cases, the document sources, and the
success criteria for the first month. The customer names a technical owner and
a business owner who approve the final go-live.

## Environment setup

The customer receives a sandbox tenant on the day of the kickoff call. API keys
for the sandbox are valid for 30 days and are rotated before production access
is granted. Production access is enabled after a security review.

## Single sign-on

Single sign-on is configured within five business days when the customer uses a
supported identity provider. SAML 2.0 and OpenID Connect are supported. Just in
time provisioning is enabled by default for the Enterprise plan.

## Document ingestion

The onboarding engineer helps load the first corpus. Documents are chunked,
embedded, and indexed in the background. A typical corpus of 5,000 pages is
indexed within four hours. Customers are encouraged to test retrieval quality
with at least 20 representative questions.

## Go-live checklist

Go-live requires a passing smoke test on the production tenant, a named on-call
contact, and an agreed escalation path. The 14-day program ends with a review
meeting where the team compares the first month metrics against the stated
success criteria.
