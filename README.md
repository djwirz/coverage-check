# Coverage Check

Checks insurance coverage in bulk against a payer API that's slow, rate-limited and often fails. Every worker shares one rate limit, so the payer's limit is never exceeded, and every patient ends in exactly one honest state: covered, not covered, or an exception a person can work through. Results are FHIR R4. It runs on Kubernetes and keeps those promises while its pods are killed and scaled mid-batch.

**Status:** planning. Design: [docs/spec.md](docs/spec.md) and [docs/adr/](docs/adr/). Synthetic members and a mock payer only. No real patient or payer data.
