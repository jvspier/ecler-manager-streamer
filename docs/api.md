
## HTTP API

Everything the UI does is available directly. With a login configured, pass
credentials as Basic auth (`curl -u user:pass …`); without one, no auth is
needed.

| Method | Path | Body | Purpose |
|---|---|---|---|
| `GET` | `/api/state` | – | full fleet snapshot + event log |
| `GET` | `/api/receivers/<id>/raw` | – | last raw device replies |
| `POST` | `/api/receivers/<id>/channel` | `{"group_id": 2}` | switch, with read-back verification |
| `POST` | `/api/receivers/<id>/expected` | `{"group_id": 2}` | set + persist expected channel |
| `POST` | `/api/receivers/<id>/bounce` | – | re-acquire: bounce to a spare channel and back |
| `POST` | `/api/receivers/<id>/hold` | – | hold the screen dark until released |
| `POST` | `/api/receivers/<id>/release` | – | restore a held screen |
| `POST` | `/api/release-all` | – | restore every held screen |
| `POST` | `/api/receivers/<id>/reboot` | – | reboot the receiver |
| `POST` | `/api/discover` | `{"ranges":[…]}` | scan for VEO devices not in the config |
| `POST` | `/api/discovered/add` | `{"ip":"…"}` | add a discovered device as a receiver |
| `GET` | `/api/setup` | – | what is on the factory-default address |
| `POST` | `/api/setup` | ip, netmask, gateway, name, … | commission it and add it to the fleet |
| `POST` | `/api/receivers/<id>/device-name` | `{"name":"…"}` | set the name stored in the device |
| `POST` | `/api/receivers/<id>/address` | ip, netmask, gateway | move it, and follow it in the config |
| `POST` | `/api/repair` | – | fix every drifted receiver |
| `POST` | `/api/batch/move` | `{"receiver_ids":[…],"group_id":6,"set_expected":true}` | switch many at once, in the background; progress in `/api/state` → `batch` |
| `POST` | `/api/channels` | group_id, name, transmitter_ip, multicast_group, note, show_button | add or edit a channel |
| `POST` | `/api/channels/<id>/delete` | – | remove a channel no receiver expects (409 otherwise) |
| `POST` | `/api/refresh` | – | poll now instead of waiting |
| `GET` | `/api/health` | – | liveness; never requires a login |
| `GET` | `/api/inventory` | – | current names as a `devices.txt` |
| `GET` | `/api/config` | – | complete config, as a downloadable backup |
| `POST` | `/api/config` | config JSON | restore a config; validated, previous one backed up |
| `GET` | `/api/whoami` | – | who is signed in |
| `POST` | `/login` / `/logout` | form | sign in / out (browser flow) |

So a morning reset is just:

```bash
curl -X POST http://127.0.0.1:8477/api/repair
```

---
