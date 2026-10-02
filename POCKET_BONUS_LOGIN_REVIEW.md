# Pocket Bonus public login implementation review

Observed 2026-10-02 at https://pocket-bonus.pages.dev/login/ through the rendered
page's inline scripts. This is a frontend review, not an audit of private server
code or a successful PocketFM account login. No phone number, OTP, token or
account credential was submitted during this review.

## Observed request contracts

All three paths are relative to **pocket-bonus.pages.dev**, not pocketfm.com.

| UI action | Request | JSON fields | Frontend success condition |
| --- | --- | --- | --- |
| Send OTP | POST /api/user/send_otp | country_code, phone_number | HTTP success and response.success |
| Verify OTP | POST /api/user/verify_otp | country_code, phone_number, otp | HTTP success and response.success |
| Refresh-token login | POST /api/user/login | refresh_token | HTTP success and response.success |

Requests declare application/json. Verification expects access_token and
refresh_token in the response. Token login expects access_token and reuses the
submitted refresh token. These are observations of what the frontend expects,
not verified guarantees about the backend response.

## Function-by-function findings

- The initial guard redirects to `/` if localStorage contains `access-token`.
  Presence is not a server-side validity or entitlement check.
- Tab handlers switch between the phone form and refresh-token form.
- Send OTP validates a nonempty phone number, disables its button, posts to
  send_otp, then displays the OTP form after a successful response.
- Verify OTP concatenates six fields, checks length, posts phone/country/OTP,
  stores returned tokens, then redirects to `/`.
- Token login posts a refresh token, stores the returned access token and
  supplied refresh token, then redirects to `/`.
- Phone input is digit-only and limited to 10 characters in this UI; country
  code has a separate field. OTP fields handle typing, paste and backspace.
- Buttons are disabled while their respective requests are in flight. Backend
  throttling, resend cooldown, attempt limits, token expiry/revocation and
  challenge binding cannot be established from the frontend.
- `showAlert` interpolates messages into innerHTML. Prefer textContent for
  untrusted error strings in our own implementation.
- Tokens are stored in localStorage. Our multi-user integration should instead
  isolate each Telegram user's account session on the server, protect storage,
  and never persist OTPs or expose provider tokens in chat/browser logs.

## What remains unknown

The underlying PocketFM URLs, headers, authentication requirements, refresh
semantics, account identity verification, purchased episode entitlements,
bonus/unlock implementation, and authorization to reuse this site's service
are not exposed by this login HTML. No post-login unlock code was inspected.
These endpoint names must not be presented as official PocketFM API endpoints.

## Integration decision

Do not silently forward users' phone numbers, OTPs or refresh tokens to this
third-party service. Copying the frontend request paths into DDownloader would
only call nonexistent local routes and would not implement PocketFM login.

Before enabling real login, implement a verified provider adapter and test the
send/verify/refresh flow through the user's own authorized account. Validate
Telegram Mini App identity on each sensitive request, isolate account sessions
by Telegram user, rate-limit OTP sends and verification attempts, and validate
provider entitlement responses. Keep purchases/unlock actions explicit and
separate from login; login alone does not imply an unlock or authorize coin
spending. Never simulate provider success.

Story Studio currently provides command controls and clearly labels provider
OTP/token login, rewards and unlock integration as unconnected. Existing
operator-configured PocketFM token/cookie download support remains separate.
