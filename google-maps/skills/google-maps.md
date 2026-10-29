## Google Maps

Places, routes and map pictures through Google Maps Platform, billed to the key the admin configured.

### Places
- For "nearby", "close" or "near me", get the user's coordinates first if a location tool is available, then search with them (`maps_search_nearby`); otherwise ask for a location or area. Free text ("sushi in Kifisia") goes to `maps_search_places`.
- Suggest several options (3-5) with name, rating and address; fetch `maps_place_details` when the user wants the phone number, opening hours or website. Let the user choose.

### Routes
- `maps_directions` takes `mode` driving, walking, bicycling or transit, and a `departure_time` or `arrival_time` (ISO) for "when should I leave" questions. For public transport use `transit`; give the lines, the changes and the total time, not every step.
- Several stops: `maps_plan_route` (it can reorder the stops for the shortest trip; not for transit).

### Links the user can open
After a route or a place, give a Google Maps link (it opens the app on a phone), URL-encoding each value:
- Route: `https://www.google.com/maps/dir/?api=1&origin=<from>&destination=<to>&travelmode=<driving|walking|bicycling|transit>`
- Stops in between (driving, walking, bicycling): add `&waypoints=<stop1>%7C<stop2>`, in visiting order; a phone takes at most 3, a computer 9, so split a longer trip into several links.
- Transit cannot take stops in between (Google answers "couldn't calculate"): give one transit link per leg.
- A place: `https://www.google.com/maps/search/?api=1&query=<name and area>&query_place_id=<place_id>`
- Never put the API key in a link, and never link a `maps.googleapis.com` address.

### Local ranking
`maps_local_rank_tracker` runs one billed search per grid point and keyword: a 3x3 grid (the default) is 9 searches, a 7x7 grid with 3 keywords is 147. Use it only when the user asks how a business ranks across an area, and tell them how many searches it will run before you call it.

### Map pictures
`maps_static_map` draws places (markers) or a route (path) as a picture. The platform shows it in the chat and saves it under `maps/` in the workspace by itself: do not call `display_images` for it. Add the Google Maps link in your reply; the picture is not clickable.

### When a call fails
- `REQUEST_DENIED`, "API not enabled" or a permission error: tell the user which API the admin must enable on the key's Google Cloud project, and do not retry. Places tools need **Places API (New)**; routes, directions and distances **Routes API**; addresses and coordinates **Geocoding API**; map pictures **Maps Static API**; weather **Weather API**; air quality **Air Quality API**; time zones **Time Zone API**; elevation **Elevation API**.
- `OVER_QUERY_LIMIT` or a quota error: stop and tell the user; do not loop.
- Show Google Maps as the source of places and reviews, and keep a review's author with its text.
