# Client onboarding and KYC: design

Status: DRAFT for review by counsel and compliance, 2026-10-23. **This is a design, not legal advice.** Regulations named below are the ones this design is organised around; each must be checked against the current text and the entity's actual registration before anything is built for outside users. Items marked (verify) are not confirmed. Placeholders in [BRACKETS] are left for facts we do not have.

## 0. The gate before any of this is switched on

Today Stoxsy is a personal research tool used by its owner. Opening it to other people changes what it legally is. In India, giving individualised investment advice or research for a fee generally needs SEBI registration (investment adviser regulations of 2013, research analyst regulations of 2014; verify scope and any exemptions). Collecting KYC makes the entity a custodian of sensitive personal data under the Digital Personal Data Protection Act, 2023 and its rules (verify the notified commencement dates). So:

| Route | What it means for onboarding |
|---|---|
| **A. Register** as an investment adviser and/or research analyst | Full client onboarding per this document: KYC, risk profiling, suitability, client agreement, fee rules, record-keeping, complaints |
| **B. Partner** with a registered entity | The partner owns client onboarding; Stoxsy receives only what it needs, under a data-processing agreement |
| **C. Stay personal-use** | No clients. Onboarding is not built. The owner's own profile is the only profile |

**No external person is onboarded until the route is decided in writing.** Everything below can be built and tested with synthetic users first.

## 1. Principles

1. **Nothing preset.** Every field is entered by the person. No default amounts, goals, risk answers or consents; checkboxes start unticked.
2. **Collect the minimum, keep the minimum, for the shortest time the law allows.**
3. **Sensitive identifiers never live in our application database in clear text.** Verification vendors and government rails do the matching; we store the outcome, a reference, and only the fields the law requires us to keep, field-encrypted.
4. **Consent is specific, separate, recorded and withdrawable.** No bundled consent.
5. **Every KYC decision is explainable and auditable** (who, what evidence, which rule, when).
6. **A person who cannot complete KYC is never silently degraded to a worse service**: they are told what is missing and what they can still do.
7. **No trading, no order placement, no custody of money.** Stoxsy never holds client funds or securities; a broker link (read-only market data or the user's own broker session) is separate and optional.

## 2. Who can be a client (initial scope)

| Segment | Initial stance |
|---|---|
| Resident individual, 18 or over, own PAN | In scope |
| Joint holders | Later |
| Minor, HUF, NRI/OCI, company, trust, partnership | Out of scope until counsel confirms; the form must stop politely and say so |
| Politically exposed persons and high-risk categories | Allowed only after enhanced due diligence and compliance sign-off |
| Residents of sanctioned jurisdictions | Not onboarded |

## 3. Access tiers (what each level of KYC unlocks)

| Tier | Requires | Unlocks | Does not unlock |
|---|---|---|---|
| T0 Visitor | Email or mobile verified (OTP) | Public pages, product tour, glossary, the T&C and privacy notice | Any personal calculation saved to an account |
| T1 Member | T0, age and residency declaration, **T&C and privacy consent**, mode chosen | Tools run on the member's own inputs: risk profile, goals, projections, fund screens; data stays private to the member | Any personalised recommendation, any advisory statement |
| T2 Verified client | T1 + full KYC (section 4) + risk profiling and suitability (section 6) + signed client agreement (section 7) | Personalised plans and, if the legal route allows, advisory output | Execution of any kind |
| T3 Linked | T2 + optional broker/aggregator link by the client's own authorisation | Reading the client's holdings to avoid manual entry | Placing orders (never offered) |

## 4. KYC journey

```
 1 Account  ->  2 Consents  ->  3 Identity (PAN)  ->  4 Address and ID proof  ->  5 KRA / CKYC check
   ->  6 Liveness and selfie  ->  7 Bank account  ->  8 FATCA / CRS and tax residency  ->  9 Screening (sanctions, PEP, adverse media)
   ->  10 Risk categorisation  ->  11 Risk profiling and suitability  ->  12 Client agreement and e-sign  ->  13 Review and activation
```

| Step | What the person does | What the system does | Evidence kept | Fails when | Then |
|---|---|---|---|---|---|
| 1 Account | Verifies mobile and email by OTP; sets a strong password and an authenticator app | Throttles attempts, checks breached passwords | OTP receipts, device and IP hash | OTP not verified | stays T0 |
| 2 Consents | Reads and separately accepts: Terms, Privacy Notice, KYC data use, each optional purpose | Stores the exact text version, time, IP, method | Consent artefact per purpose | any mandatory consent declined | cannot proceed; explained |
| 3 Identity | Enters PAN and name | PAN verified against the income-tax database through an authorised provider; name and date-of-birth match with a tolerance rule; PAN-Aadhaar link status read | Provider reference id, match score, result (not the PAN in clear in logs) | invalid or inoperative PAN, name mismatch | correction path, then manual review |
| 4 Address and ID proof | Chooses DigiLocker (preferred) or an officially valid document upload | DigiLocker pull of Aadhaar-derived data or other documents; **masked Aadhaar only**; offline-XML route allowed with share code. Documents verified for authenticity and tampering | Document hash, vendor verdict, masked identifier, issue date | unreadable, expired, tampered, address mismatch | resubmit or manual review |
| 5 KRA / CKYC | Nothing (consent already given) | Looks up the SEBI KYC Registration Agencies and CKYC (CERSAI) by PAN. If a valid KYC exists, fetches it and asks the person to confirm and fill gaps; if not, runs the full KYC and files it | KRA/CKYC reference, status, fetch time | KRA record on hold or rejected | fix at the KRA, or re-KYC |
| 6 Liveness and selfie | Takes a live selfie with a randomised prompt | Liveness and face match against the ID photo; geotag and time check; in-person verification equivalence via video where the regime allows (verify) | Match score, liveness verdict, image hash (image in encrypted vault) | spoof suspected, low match | retry limited; manual video review |
| 7 Bank account | Enters account number and IFSC | Penny-drop or reverse-penny-drop; name match with PAN name | Verification reference, matched name | account closed, name mismatch | alternative bank, manual |
| 8 FATCA / CRS | Declares tax residency, country of birth, US-person status | Validates logic, flags reportable cases | Self-certification, timestamp | declares non-India residency | out of initial scope; polite stop |
| 9 Screening | Nothing | Screens against UN, OFAC and Indian lists, PEP lists, adverse-media sources, at onboarding and then ongoing | Match list, reviewer decision | any potential match | compliance officer review; no auto-reject on a name hit alone |
| 10 Risk categorisation | Nothing | Scores AML risk low, medium or high from the signals above (residency, occupation, PEP, source of funds where asked, channel) | Score and rule version | high | enhanced due diligence: income or net-worth proof, source of funds, senior approval |
| 11 Risk profiling and suitability | Answers the questionnaire (the Investor-mode questions generalised: horizon, income stability, dependents, tolerance, knowledge, experience, goals, existing assets, liabilities) | Computes capacity and tolerance; records the lower; flags unsuitable requests; no preset answers | Questionnaire version, answers, derived profile, date | incomplete | cannot reach T2 |
| 12 Client agreement and e-sign | Reads the advisory/research agreement, fee schedule and disclosures; signs with an authorised e-sign service | Stores the signed PDF hash, certificate, time | Signed document, certificate chain | not signed | stays T1 |
| 13 Review and activation | Sees a summary of what was verified and what was stored | Maker-checker: a second staff member approves any case with a flag; activates T2 | Approval record | unresolved flags | pending with reasons |

### Re-verification and ongoing duties
Periodic KYC refresh on a risk-based cycle (the RBI master direction pattern is high every 2 years, medium every 8, low every 10; verify what applies here); event-triggered refresh when a name, address, PAN status or sanction list changes; ongoing monitoring for unusual changes; the person can update contact details at any time through a verified channel.

## 5. Data model (new tables; nothing below exists yet)

| Table | Purpose | Notes |
|---|---|---|
| `clients` | one row per person: status, tier, risk category, created, activated | 1:1 with an existing `users` login |
| `consents` | purpose, text version, granted/withdrawn, time, method, evidence hash | append-only |
| `kyc_cases` | one case per attempt: state machine, reviewer, outcome, rule version | never edited in place |
| `kyc_checks` | each step's result: vendor, reference id, verdict, score, time | no raw identifiers |
| `kyc_documents` | encrypted vault pointer, hash, type, expiry, retention-until | files in an encrypted object store, keys in a KMS |
| `kyc_identifiers` | PAN and the masked ID, **field-level encrypted**, access logged | only the verification service reads them |
| `risk_profiles` | questionnaire version, answers, capacity, tolerance, derived profile | extends the Investor questionnaire |
| `agreements` | document version, signed hash, certificate, time | |
| `screening_hits` | list, match strength, reviewer decision, rationale | |
| `kyc_audit` | who saw or changed what | append-only; a reviewer opening a document is logged |
| `erasure_and_retention` | per record: legal basis to keep, delete-after date | drives scheduled purge |

Roles: `client`, `reviewer` (maker), `approver` (checker), `compliance_officer`, `admin`, `viewer`. No role can read raw identifiers except the narrow verification service; reviewers see masked values and open documents with a logged "break glass" reason.

## 6. Risk profiling and suitability

The Investor-mode questionnaire becomes the first version: capacity (horizon, income stability, dependents) and tolerance (reaction to a fall), the profile being the lower of the two and never raised to rescue a goal. For clients add knowledge and experience, liabilities, existing assets, liquidity needs and objectives. Each output states the profile and the reasons, and any product or action that exceeds it is blocked and explained. Re-profile at least every [N] months and on a major life or market event the client reports.

## 7. Agreement, fees and disclosures

Plain-language agreement with: scope of service, what is not provided (no execution, no custody, no guarantee), fee schedule, conflicts of interest, risk disclosures, data use, grievance route and the regulator's complaint channels, termination. Fee caps and mandatory clauses are set by the regulations for the chosen route (verify). Before signing, the client sees the register entry number [SEBI REGISTRATION NO.] and the compliance officer's contact.

## 8. Security and privacy controls

Encryption in transit and at rest; field-level encryption and a KMS for identifiers; documents in a separate encrypted store; strict least-privilege; MFA for staff, mandatory for approvers; IP allow-list for the KYC back office; immutable audit trail; secrets out of code and logs; vendor due diligence and data-processing agreements; data stored in India (verify localisation expectations); backup encryption; breach runbook with the regulator and affected-person notification clock (verify current timelines); no production KYC data in test environments (synthetic data only); retention per section 9.

## 9. Retention and erasure

| Record | Keep for | Why |
|---|---|---|
| KYC records and transaction-type records | the period the AML rules require after the relationship ends, commonly five years (verify the exact rule and trigger) | PMLA and its record-keeping rules |
| Advisory records (profile, recommendations, agreement, communications) | five years or as the adviser/analyst regulations require (verify) | SEBI record-keeping |
| Marketing and optional-purpose data | until consent is withdrawn | DPDP consent |
| Raw selfie and ID images | as short as the KYC rule allows after verification; keep hashes and vendor references longer | minimisation |
| Everything else | deleted on request unless a law requires retention; the person is told what is kept and why | DPDP erasure right vs retention duties |

## 10. Screens (wireframe level)

Progress bar of the 13 steps; one task per screen; plain language; the reason for each question shown beside it; save-and-resume; upload with on-device quality hints; a visible "why we ask" drawer; a status page after submission with exactly what is pending; a support route and a grievance link on every screen; accessibility to WCAG 2.2 AA; works on a phone; English first, other languages later. Every journey has an animated "what happens next" explainer (standing rule for reports and flows).

## 11. Failure modes to design for

Vendor down (queue and resume, never "approve on timeout"); name variants and transliteration; a person with no DigiLocker or Aadhaar link; duplicate or synthetic identities (device and behaviour signals, same-bank and same-phone clustering); document forgery; account takeover after KYC (step-up on sensitive changes); a reviewer error (maker-checker, sampling audit); wrongful rejection (appeal path with a human); data subject requests during an open case; regulator inspection (a one-click evidence pack per client).

## 12. Build and test plan

1. Synthetic-person test harness and a fake verification provider (no real data).
2. State machine, consents, audit tables and APIs with property tests (a case can never skip a step; consent withdrawal stops dependent processing).
3. Vendor adapters behind one interface, sandbox keys only.
4. Back-office review UI with maker-checker.
5. Security review, data-protection impact assessment, counsel review of every text.
6. Pilot with the owner's own account end to end, then a closed pilot only after the legal gate.

## 13. Open decisions

1. The legal route (A, B or C) and the registered entity's name, address and registration numbers.
2. Which verification vendors (PAN, DigiLocker aggregator, penny-drop, screening, e-sign); selection needs quotes, licences and data-residency terms.
3. Whether to support video KYC and in-person verification equivalents.
4. Client segments beyond resident individuals.
5. Grievance officer and compliance officer appointments.
6. Fee model (it determines which advisory rules apply).
