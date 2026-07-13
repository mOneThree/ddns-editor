# Enhancement backlog

Feature ideas discussed and deliberately deferred, so they don't get lost.
For the provider-schema backlog specifically, see `PROVIDER_BACKLOG.md`.

## Bulk operations
- [ ] Import multiple records at once (paste a JSON array or CSV) instead of one at a time
- [ ] Duplicate/clone an existing record as a starting point for a similar one
- [ ] Full export/import bundle (config + backups + activity) for moving to a new host

## Deployment reach
- [ ] Unraid Community App template
- [ ] Kubernetes manifests (ddns-updater itself ships k8s examples; this doesn't have an equivalent yet)

## Observability
- [ ] Distinguish "last update succeeded" from "last update failed" in the status column -- right now it only shows last-known IP/time, not whether the most recent attempt actually worked (rate-limited, bad auth, etc.)
- [ ] Prometheus-style `/metrics` endpoint (record count, last save time, failed-login count) for homelab monitoring stacks (Grafana etc.)

## Data integrity
- [ ] JSON Schema validation on the Advanced tab -- catch structural mistakes beyond "is it valid JSON" (missing `provider` key, duplicate domains) before saving

## Notes
Nothing here is prioritized relative to anything else -- just captured so it's available when there's appetite to pick one up. Multi-user/roles/2FA, the token-based API + webhooks, the interface redesign (sidebar nav, blue/dark theme, card layouts), and version tracking/update notifications (previously listed here) have since been built; see README.md.
