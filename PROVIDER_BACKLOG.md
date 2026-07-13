# Provider backlog

The Add/Edit form currently supports 10 of ddns-updater's ~45 providers
(schema-driven -- see `PROVIDER_SCHEMAS` in `app.py`). Everything else
still works via the **Advanced (Raw JSON)** tab, it just doesn't have a
friendly form yet.

Adding a provider from this list means:
1. Pull the exact field names from `https://github.com/qdm12/ddns-updater/blob/master/docs/<name>.md`
   (don't guess -- a wrong field name saves "successfully" but silently
   fails inside ddns-updater itself)
2. Add one entry to `PROVIDER_SCHEMAS` in `app.py` -- no template changes needed,
   the form renders itself from the schema
3. If the provider has a read-only API endpoint (account info, zone lookup,
   etc. -- something that can't accidentally trigger a real DNS update),
   consider wiring up a `test_<provider>()` function too, same pattern as
   Cloudflare/DigitalOcean/GoDaddy/Porkbun

## Already supported
Cloudflare, Duck DNS, NoIP, Name.com, DigitalOcean, GoDaddy, Dynu, Porkbun,
Namecheap, OVH (DynHost mode only)

## Backlog (alphabetical, unprioritized)

- [ ] Aliyun
- [ ] AllInkl
- [ ] ChangeIP
- [ ] DD24
- [ ] DDNSS.de
- [ ] deSEC
- [ ] Domeneshop
- [ ] DonDominio
- [ ] DNSOMatic
- [ ] DNSPod
- [ ] Dreamhost
- [ ] DynDNS
- [ ] DynV6
- [ ] EasyDNS
- [ ] FreeDNS
- [ ] Gandi
- [ ] GCP (Google Cloud DNS)
- [ ] GoIP.de
- [ ] He.net
- [ ] Hetzner (legacy API)
- [ ] Hetzner Cloud
- [ ] Infomaniak
- [ ] INWX
- [ ] Ionos
- [ ] ipv64
- [ ] Linode
- [ ] Loopia
- [ ] LuaDNS
- [ ] Myaddr
- [ ] NameSilo
- [ ] Netcup
- [ ] Now-DNS
- [ ] Njalla
- [ ] OpenDNS
- [ ] OVH -- API mode (app_key/app_secret/consumer_key), in addition to the
      existing DynHost mode
- [ ] Route53 (AWS)
- [ ] Scaleway
- [ ] Selfhost.de
- [ ] Servercow.de
- [ ] Spaceship
- [ ] Spdyn
- [ ] Strato.de
- [ ] Variomedia.de
- [ ] Zoneedit

## Prioritization notes
No particular order yet -- add the ones you (or whoever's using this)
actually need first. Worth grouping by field shape too, since providers
sharing a shape (e.g. plain username+password like NoIP/OVH, or
key+secret like GoDaddy/Porkbun) are near-zero effort once the doc page
is checked.
