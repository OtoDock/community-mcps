## Showing Home Assistant cameras to the user

`ha_get_camera_image` returns a snapshot **to you** for visual analysis — it is **not** shown to the user. And a raw camera URL (`/api/camera_proxy/...` on the Home Assistant host) passed to `display_images` is fetched by the **user's browser**, so it renders as a **broken image** whenever the user is away from the local network.

To actually **show** the user a camera snapshot (works on any network), download it to your workspace first, then display the saved file:

1. **Get the camera's pre-signed snapshot URL.** Read the camera entity's state/attributes and take its **`entity_picture`** — a path like `/api/camera_proxy/camera.outside_camera?token=…`. The embedded token authorizes the snapshot on its own (Home Assistant rotates it every few minutes), so you need **no** Home Assistant token of your own. Read it fresh right before downloading.
2. **Download it into your workspace**, prefixing your Home Assistant base URL (the same host the Home Assistant tools use):
   ```
   curl -sk "<HA_BASE_URL><entity_picture>" -o workspace/outside-camera.jpg
   ```
3. **Display the saved file:**
   ```
   display_images(images=[{"source": "workspace/outside-camera.jpg",
                           "caption": "Outside camera — just now"}])
   ```
   The platform serves the saved file to the user regardless of their network.

**Use `ha_get_camera_image`** only when you need to *analyze* what a camera sees ("is someone at the door?", "did the package arrive?"). **Use the download-then-display steps above** when the user wants to *see* the camera. **Never** pass a raw `camera_proxy` URL — or any `http://192.168.*` / `10.*` local-network URL — directly to `display_images`.
