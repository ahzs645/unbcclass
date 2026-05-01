# UNBC Course Catalogue Scraping Notes

## Current Result

The public UNBC course catalogue exposes terms back to **2022 January Semester** (`202201`).

For CPSC, a completed all-term scrape produced:

- `34` available term values in the dropdown
- `29` terms with CPSC records
- `1,784` CPSC course records
- earliest term with records: `202201` / `2022 January Semester`

Output from that run:

```text
data/cpsc-all-semesters.json
```

## Architecture

The catalogue is a Blazor Server app:

- initial HTML is server-side rendered
- `blazor.web.js` hydrates the page
- browser connects to `/_blazor/negotiate`, then a SignalR WebSocket
- UI changes are sent over the WebSocket
- the server responds with Blazor DOM diff render batches
- no useful public REST/JSON search API was found

Because of that, the working scraper uses Playwright to drive the hydrated page:

1. wait for Blazor hydration
2. select a term and subject
3. dispatch bubbling `input` and `change` events
4. click Search
5. parse rendered `#results > ul > li` records

## Pre-2022 Tests

We tested whether older terms could be forced through the public UI path.

### Direct Select Value

Tried setting unavailable term values directly:

- `202105`
- `202104`
- `202103`
- `202102`
- `202101`
- `202005`
- `202001`

Result: the browser `<select>` value became empty and the Search button disabled.

### Injected Option

Tried injecting fake `<option>` elements for older terms, selecting them, and dispatching Blazor events.

Result: this did not produce older results and destabilized the Blazor page/circuit during testing.

### URL Query Params

Tried likely query parameter forms:

- `?term=202101&subj=CPSC`
- `?termCode=202101&subj=CPSC`
- `?strm=202101&subj=CPSC`
- `?subj=CPSC&crse=100`

Result: the public route did not hydrate into a usable search state for those URLs.

### SignalR Replay

Tried a direct SignalR replay experiment:

1. intercepted the live WebSocket before Blazor loaded
2. performed a real term change to `202601`
3. captured the outgoing binary Blazor event frame
4. byte-patched both occurrences of `202601` to `202101`
5. sent the patched frame back over the same open WebSocket
6. selected CPSC normally and clicked Search

Observed output:

```text
patched-send {'ok': True, 'frameLength': 175, 'frameCount': 9, 'replacements': 2}
button-disabled False
result count: 66
firstHeading: Sections offered for the 202601 semester
```

The patched SignalR frame was sent successfully, but the server still returned `202601` results.

Likely reasons:

- Blazor rejected or ignored the patched event
- the component validates term values against its loaded option set
- a later valid component state overwrote the fake term before Search

## Conclusion

The public Blazor catalogue path does **not** appear to expose pre-2022 data, even with DOM manipulation or basic SignalR frame replay.

The practical floor from this app is:

```text
202201 / 2022 January Semester
```

To get older data, we would need a different source:

- internal Banner/SSB access
- an authenticated/internal API behind the observed `503` endpoints
- registrar exports
- archived catalogue pages
- database/reporting access

