# host-gateway example

A client container reaches a sibling's **published** port through
`host.docker.internal:18080`, as it would with Docker.

```console
cd examples/host-gateway
wslc compose up -d
wslc compose exec client wget -qO- http://host.docker.internal:18080   # -> hello-from-api
wslc compose down                                                       # also removes the iptables chains
```

Without `extra_hosts: [...:host-gateway]` the same request to the network gateway
(`172.31.50.1:18080`) is refused: wslc publishes the port on Windows, not on the bridge.

## What was verified (wslc 3.0.3.0)

| Check | Result |
|---|---|
| client → `host.docker.internal:18080` | `hello-from-api` |
| same request with no host-gateway rules (control) | `Connection refused` |
| `up` run twice | "up-to-date", iptables rules byte-identical |
| `up --force-recreate api` | DNAT target follows the new container IP, request still works |
| `stop api` / `start api` | rules refreshed, request still works |
| `down` | all `WCG*` chains and jumps removed from the session |

Inspect the rules inside the session (admin or not, same elevation as your containers):

```console
wslc system session run sh -c 'iptables -t nat -S | grep WCG; iptables -S | grep WCG'
```
