# Postman collection

`collection.json` is generated from the application's OpenAPI schema, so it
covers every route the running code mounts — public, authenticated (org),
billing, and admin.

## Import

1. Postman → **Import** → `postman/collection.json`.
2. Import `postman/environment.json` and select the **ESG Chatbot - Local**
   environment.

## Variables

| Variable  | Meaning                                                                 |
| --------- | ----------------------------------------------------------------------- |
| `baseUrl` | Server origin, e.g. `http://localhost:8000`.                            |
| `token`   | A user bearer token from `POST /auth/login` or `/auth/signup`.          |
| `apiKey`  | The value of `API_KEY` in `.env`, used by the admin routes.             |

The generated collection sends `{{token}}` as `Authorization: Bearer` on the
authenticated routes and `{{apiKey}}` on the admin routes. Keep both secrets in
the environment, never in the collection file.

## Typical order

1. `POST /auth/signup` (or `POST /auth/login`) → copy `token` into the
   environment. Free accounts must confirm their email
   (`ALLOW_LEGACY_CONTENT` unrelated); an admin can verify with
   `POST /admin/users/{user_id}/verify`.
2. `GET /me` → inspect `organisation`, `free_tier`.
3. `POST /chat` → ask a question (402 once the free-tier cap is reached).
4. Org console: `GET /org/overview`, `GET /org/usage`, `GET /org/members`,
   `GET /org/invites`, `GET /org/plans`.
5. Billing: `GET /billing/usage`, `POST /billing/checkout`,
   `POST /billing/portal`. `POST /billing/webhook` is signature-verified by
   Stripe, so it is not usefully callable by hand.
6. Admin: set `apiKey`, then `GET /admin/status`, `GET /admin/organisations`,
   `GET /admin/usage`, and so on.

## Regenerate

```bash
postman/generate.sh
```

Requires the repo virtualenv (`venv/`) and Node's `npx`. The intermediate
`openapi.json` is a build artifact and is git-ignored.
