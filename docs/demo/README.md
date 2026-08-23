# Synthetic demo asset

`synthetic-session.svg` is a deterministic documentation preview, not a captured OpenClaw conversation and not a record of market performance.

Its only input is `synthetic-market-data.json`. The values and symbols were invented for this repository and contain no real account, holding, watchlist, chat identity, notification destination, local path, or live timestamp. The skipped alert intentionally demonstrates the plugin's fail-closed behavior when a notification target is absent.

Regenerate and verify it with:

```bash
npm run demo:generate
npm run demo:check
```

When changing the asset, edit the JSON or generator, regenerate the SVG, inspect the rendered image, and run the privacy audit. Do not replace it with a screenshot from a real chat or runtime state.
