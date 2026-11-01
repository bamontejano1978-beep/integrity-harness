# Integrity Evidence — Local Offline Verifier

This local Gradio UI verifies a saved Meta held-out evidence package, can run a tamper test against a temporary copy, and can replay one paired case from the signed archive. It makes **no Meta API calls or other network requests**, requires no API key, and accepts only small ZIP uploads.

From the submission root, install the pinned UI dependency and launch it:

```bash
python -m pip install -r offline_verifier/requirements.txt
python offline_verifier/app.py
```

Open the local URL printed by Gradio. Upload `evidence/meta-heldout-live-20261028-signed.zip`, click **Verificar paquete**, select a held-out case, then click **Mostrar recorrido del caso**. The walkthrough shows the user turns, saved model response without the guard, original proposal and final response with the guard, rule verdicts, and mock handoff provenance. Do not expose the server to a public network; this is a local review and recording aid, not a hosted demo.

## Input limits

- ZIP upload maximum: 8 MiB compressed, 5 MiB uncompressed.
- Maximum 110 entries; at most 1 MiB per entry; maximum compression ratio 100.
- The uploaded archive is not modified; tamper-test makes a temporary copy.
- The app displays only the selected case's saved turns and responses after verifying the signed archive; it does not display the full report. The walkthrough is a replay of saved evidence, not a fresh model run. It states that the pressure labels are supplied scenario metadata, guarded responses are deterministic, and handoff tools are local mocks with no real-world side effects.

## What a valid signature means

A valid Ed25519 signature and matching manifest verify the package's mathematical integrity. The public key is embedded in the package and is self-described; this does not establish the signer's identity or a trusted timestamp. A legacy package without a signature can only pass the manifest hash and structural checks.
