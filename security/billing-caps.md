# Billing caps and alerts (owner rule R60)

One row per paid service. Cap = the hard spending limit set at the provider; where the provider has
none, write "alert only" and fill the alert column. Re-verify every 90 days: open the URL, read the
number, update the date. `check-security --rule R60` reads this file.

What each row is based on (2026-09-17, read from the code and the production `.env` key NAMES,
never values):

- **openai**: `OPENAI_API_KEY` is set on the box and used by ONE thing, the Discord bot's replies
  (`afcbot/bot.py`, with Groq / Gemini fallbacks). The OCR path left OpenAI on 2026-07-09. The cap
  and alert live on the owner's OpenAI account; nobody in this session could open that page, so the
  row stays `?` until the owner reads it. Until then the checker reports R60 HIGH on purpose.
- **google-ai**: AFC's Gemini key runs on the FREE tier with no billing account: the API itself
  refuses past 20 reads per model per day (seen 2026-09-12, `RESOURCE_EXHAUSTED`), which is a hard
  cap of zero spend. Organizers' own keys (BYOK, 2026-09-13) are their bill, not AFC's.
- **deepl**: `DEEPL_API_KEY` on the free plan; the journal is full of `Quota exceeded`, which is the
  plan's hard stop. Not detected by the checker (no rule knows DeepL), listed anyway.
- **stripe**, **paystack**: payment processors. AFC never prepays; their fees are deducted from each
  payment, so there is no spend to cap at the provider. Paystack's balance is transit, not a wallet
  (2026-09-07). Alerts are the providers' own dashboards, on the owner's accounts.
- **aws**: `boto3` is in requirements but no module imports it, and no AWS key is in the production
  environment. The account was archived 2026-09-11 and the boxes are to be terminated after
  2026-09-25 (OWNER-RUNBOOK). No spend is possible from this code.
- **gcp**: `google-cloud` and `google-cloud-vision` are in requirements but no module imports them,
  and no GCP credential is in the environment. No spend is possible from this code. Remove the two
  packages on the next dependency pass.

| Service | Cap | Alert at | Where set (URL) | Verified on | By |
|---|---|---|---|---|---|
| openai | ? | ? | https://platform.openai.com/settings/organization/limits (the owner's account; the bot is the only caller) | 2026-09-17 | owner to fill |
| google-ai | free tier, no billing account: the API refuses past 20 reads per model per day | n/a: no billing account | https://aistudio.google.com/app/apikey and https://console.cloud.google.com/billing | 2026-09-12 | Claude, from the API's own refusal in the production journal |
| deepl | free plan quota (the API refuses with "Quota exceeded") | n/a: free plan | https://www.deepl.com/account/usage | 2026-09-17 | Claude, from the production journal |
| stripe | no prepaid spend: fees are deducted from each payment, no cap exists at the provider | Stripe dashboard alerts on the owner's account | https://dashboard.stripe.com/settings | 2026-09-17 | Claude, from the code (afc_shop/stripe_checkout.py) |
| paystack | no prepaid spend: fees are deducted from each payment, no cap exists at the provider | Paystack dashboard alerts on the owner's account | https://dashboard.paystack.com/#/settings | 2026-09-17 | Claude, from the code (afc_shop/paystack_payout.py) |
| aws | none possible: no key in the environment, no code imports boto3, account archived 2026-09-11 | n/a | https://console.aws.amazon.com/billing (archived account) | 2026-09-17 | Claude, from the environment and the code |
| gcp | none possible: no credential in the environment, no code imports google.cloud | n/a | https://console.cloud.google.com/billing | 2026-09-17 | Claude, from the environment and the code |
