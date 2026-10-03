# Expanded ground-truth source manifest

**Evidence closure as of 2026-10-03:** 16 of 23 frozen references have
sufficient retained primary evidence; 7 remain insufficient. The per-license
notes below record the official query, returned fields, first-party corroboration
where available, and access limitations.

## Method and evidence limits

Board or licensing-authority records are the primary evidence that a license
exists. Registry records, official DBAs or principals, first-party websites,
and exact business-name/address/phone observations support the link from that
license to the frozen business identity. Fuzzy similarity alone was not enough
to establish a business or accept a license. CAPTCHA, WAF, Turnstile, HTTP
errors, and empty searches were not treated as confirmed negatives.

Shared licenses across multiple phones are listed once by global license ID;
their `case_ids` are recorded on that item. The original eight-license set was
the conservative verified subset available at the original submission time.
The expanded 23 were manually adjudicated across all 17 unique valid phones.
Texas registry expansion remained out of scope, but Texas licenses remain in
the expanded denominator. The frozen values in
[`data/evaluation_ground_truth_expanded.json`](../data/evaluation_ground_truth_expanded.json)
are not changed by this manifest.

| Scope | Count |
|---|---:|
| All globally unique licenses | 23 |
| Texas: TDLR and TSBPE | 10 |
| Maryland, DC, Virginia, and California | 13 |

This manifest reports only evidence retained in this repository and its
evaluation artifacts. A frozen ground-truth entry records an adjudicated value,
but by itself is not the underlying official capture. The five references
closed in this pass have concise manual evidence records below. Seven of the
originally unlinked references remain unsupported or inaccessible; those gaps
are marked rather than filled by inference. For recovered references, the
current expanded report retains the official candidate fields, source
reference, source URL, and fetch time.

## License records

### DPOR:2705019835

- **Evidence status:** VERIFIED AND RETAINED.
- **Case(s):** P02. **Frozen identity:** John C. Flood. **Official holder:**
  JOHN C FLOOD OF VIRGINIA INC.
- **Official licensing source:** Virginia Department of Professional and
  Occupational Regulation (DPOR), 2705 A current regulant list. **Record
  source:** [official 2705 A list](https://www.dpor.virginia.gov/sites/default/files/Records%20and%20Documents/Regulant%20List/2705a__crnt.txt).
- **Retained evidence/date:** On 2026-10-03, the official 2705 A list was
  inspected for certificate suffix `019835`, the 2705 A component of this
  canonical DPOR number. The row identifies JOHN C FLOOD OF VIRGINIA INC,
  rank A, specialties `RBC PLB ELE GFC HVA`, certification date 1993-02-17,
  and expiration 2027-02-28. The [John C. Flood first-party site](https://www.johncflood.com/)
  explicitly publishes `VA Class A Contractor: 2705019835`. These query and
  response fields are retained here as a sanitized manual evidence note; no
  credentials or session data were used or stored.
- **Relationship, type, status:** The official business holder name and the
  first-party site's exact board-specific number support the P02 relationship.
  The official list identifies Class A and the specialties above. The source
  row was current as of the verification date; the site separately identifies
  the credential as a VA Class A Contractor.
- **Why included:** The official DPOR record and the matching exact number on
  the business's own site independently support the frozen reference.

### DC_INDUSTRIAL_TRADES:PC502

- **Evidence status:** VERIFIED AND RETAINED.
- **Case(s):** P02. **Frozen identity:** John C. Flood. **Official holder:**
  JOHN C FLOOD, INC.
- **Official licensing source:** DC Board of Industrial Trades, public OPLA
  license search. **Query endpoint:** [official exact-number search](https://govservices.dcra.dc.gov/oplaportal/Home/GetLicenseSearchDetailsByFilter).
- **Retained evidence/date:** On 2026-10-03, an ordinary public POST query for
  `licenseNumber=PC502` returned one record: licensee JOHN C FLOOD, INC;
  license type Plumber/Gasfitter Contractor; status `Inactive - Reinstatement
  Eligible`; issue date 1986-01-01; expiration 2026-03-31. The [John C. Flood
  first-party site](https://www.johncflood.com/) explicitly publishes
  `DC Contractor License: PC502` and displays the business phone
  (703) 752-1251. The source response fields and query are retained here as a
  sanitized manual note; no credentials or session data were stored.
- **Relationship, type, status:** The business's exact DC license disclosure
  matches the official license number and record. Type and status above are
  transcribed as returned by the board; they are not a claim that the license
  is currently active.
- **Why included:** The official DC record and the first-party exact-number
  disclosure establish the P02 business/license relationship.

### DC_INDUSTRIAL_TRADES:RC770

- **Evidence status:** VERIFIED AND RETAINED.
- **Case(s):** P02. **Frozen identity:** John C. Flood. **Official holder:**
  JOHN C FLOOD, INC.
- **Official licensing source:** DC Board of Industrial Trades, public OPLA
  license search. **Query endpoint:** [official exact-number search](https://govservices.dcra.dc.gov/oplaportal/Home/GetLicenseSearchDetailsByFilter).
- **Retained evidence/date:** On 2026-10-03, an ordinary public POST query for
  `licenseNumber=RC770` returned one record: licensee JOHN C FLOOD, INC;
  license type Refrig/Air Cond Contractor; status `Active`; issue date
  1993-01-01; expiration 2026-09-30. The current [John C. Flood first-party
  site](https://www.johncflood.com/) identifies its DC/Maryland services and
  displays the same business phone as its Virginia location. A search of that
  page did not find `RC770`, so no exact-number disclosure is claimed. These
  query and response fields are retained here as a sanitized manual evidence
  note. The board's `Active` status is transcribed as returned, alongside its
  stated expiration date.
- **Relationship, type, status:** The official holder's `JOHN C FLOOD` name
  exactly matches the first-party business identity after punctuation and
  corporate-suffix normalization. This is a direct legal-name relationship;
  no fuzzy-name inference is used. The type and status are transcribed above.
- **Why included:** The official DC business record and exact normalized
  first-party/legal name support the P02 reference.

### DPOR:2710061674

- **Evidence status:** NOT VERIFIED.
- **Case(s):** P03. **Frozen identity:** Brownlee Plumbing LLC. **Holder and
  type stated in the original ground truth:** JOTIS LAMAR BROWNLEE, Tradesman,
  Master Plumber (MPLB). **Expiration stated there:** 2025-09-30; status was
  recorded as unknown/not displayed.
- **Official licensing source:** DPOR. **Source checked:** [DPOR regulant
  lists](https://www.dpor.virginia.gov/RegulantLists).
- **Retained evidence/date:** On 2026-10-03, the current official DPOR
  regulant-list material was searched for `2710061674` and `JOTIS LAMAR
  BROWNLEE`; no matching current row was found. The official current list may
  omit expired records, so this absence does not establish that a license was
  never issued. `data/evaluation_ground_truth.json` retains the frozen holder,
  type, specialty, and expiration fields, but those GT fields are not an
  official record capture. No license-specific official row or result was
  retained.
- **Relationship:** The frozen GT associates this individual license with P03,
  but retained official evidence does not establish a qualifying principal
  relationship between the holder and Brownlee Plumbing LLC. No first-party
  corroboration is recorded for this number.
- **Why included / gap:** Included as a frozen P03 reference. It remains
  unverified because the current official listing did not return the record
  and no underlying record or qualifying individual-to-business relationship
  is retained. This is not a negative license finding; status remains unknown.

### DPOR:2705159268

- **Case(s):** P03. **Frozen identity:** Brownlee Plumbing LLC. **Official
  holder:** BROWNLEE PLUMBING LLC.
- **Official licensing source:** DPOR regulant list 2705 C. **Record URL:**
  [official 2705 C current list](https://www.dpor.virginia.gov/sites/default/files/Records%20and%20Documents/Regulant%20List/2705c__crnt.txt).
- **Retained evidence/date:** The expanded report retains the official
  candidate, source reference, record URL, holder name, and fetch time
  2026-10-03 03:26:41 UTC. The report's accepted candidate records the exact
  business address and state-consistent address evidence.
- **Relationship, type, status:** Exact normalized legal/business name and
  exact official address support the business relationship. The accepted
  record does not retain a license type or status value. No registry attribution
  is claimed for this recovery.
- **Why included:** The official DPOR row and exact identity evidence support
  the P03 reference.

### MD_ELECTRICIANS:14003

- **Evidence status:** SOURCE INACCESSIBLE.
- **Case(s):** P04. **Frozen identity:** PowerWorks LLC. **Holder, type, status,
  and expiration stated in the original ground truth:** BELVIN DOUGLAS MITCHELL;
  STATEWIDE MASTER ELECTRICIAN; raw status `INSURED` (normalized `active`);
  expiration 2026-12-19.
- **Official licensing source:** Maryland State Board of Electricians.
  **Source URL:** [official electrician query](https://www.dllr.state.md.us/cgi-bin/ElectronicLicensing/OP_Search/OP_search.cgi?calling_app=ME::ME_personal_name).
- **Retained evidence/date:** The original GT retains the fields above, but
  those are not a primary capture. The official query form presents a CAPTCHA
  human-check gate before the record search can run; the saved audit describes
  this access limit. No CAPTCHA was submitted or bypassed, and no official
  result for 14003 was obtained or retained. The linked official form is the
  public query entry point; no reproducible record URL is available.
- **Relationship:** No retained official evidence links the individual master
  electrician to PowerWorks LLC as a qualifying principal. The board search
  requires a human check; the pipeline does not submit the name.
- **Why included / gap:** Included as a frozen P04 reference. It remains
  unsupported because the official source could not be queried without passing
  its CAPTCHA and no qualifying relationship between the individual holder
  and PowerWorks LLC is retained.

### DC_INDUSTRIAL_TRADES:ECC40000308

- **Case(s):** P05. **Frozen identity:** JM Plumbing & Heating. **Official
  holder:** JM Plumbing & Heating.
- **Official licensing source:** DC Board of Industrial Trades. **Record
  source:** [official Industrial Trades search](https://govservices.dcra.dc.gov/oplaportal/Home/GetLicenseSearchDetailsByFilter).
- **Retained evidence/date:** The expanded report retains the official row,
  holder, raw status `Active`, normalized status `active`, source reference,
  source URL, and fetch time 2026-10-03 03:28:05 UTC.
- **Relationship, type, status:** The official holder exactly matches the
  established Places business name after normalization. The DC registry also
  retains an exact legal-name record for JM Plumbing & Heating Corporation;
  that legal-name key is not needed to explain this exact base-name match.
  The report records trade compatibility but does not retain a separate
  license-class label.
- **Why included:** The official board row and exact business-name match
  support this P05 reference. This one matched the base name and is not counted
  as a registry legal-name recovery.

### DC_INDUSTRIAL_TRADES:PC1000776

- **Case(s):** P05. **Frozen identity:** JM Plumbing & Heating. **Official
  holder:** JM PLUMBING & HEATING CORPORATION.
- **Official licensing source:** DC Board of Industrial Trades. **Record
  source:** [official Industrial Trades search](https://govservices.dcra.dc.gov/oplaportal/Home/GetLicenseSearchDetailsByFilter).
- **Retained evidence/date:** The expanded report retains the official row,
  holder, raw status `Active`, normalized status `active`, source reference,
  source URL, and fetch time 2026-10-03 03:28:05 UTC. The P05 report also
  records the official DC corporate-name result and its file-number relationship.
- **Relationship, type, status:** The established DC registry entity's legal
  name, JM Plumbing & Heating Corporation, is the normalized exact holder
  name. The report records compatible trade evidence but no separate
  license-class label.
- **Why included:** The official board row and registry legal-name join support
  this P05 reference. It is counted as a legal-name-attributed recovery.

### DC_INDUSTRIAL_TRADES:RC902397

- **Case(s):** P05. **Frozen identity:** JM Plumbing & Heating. **Official
  holder:** JM PLUMBING & HEATING CORPORATION.
- **Official licensing source:** DC Board of Industrial Trades. **Record
  source:** [official Industrial Trades search](https://govservices.dcra.dc.gov/oplaportal/Home/GetLicenseSearchDetailsByFilter).
- **Retained evidence/date:** The expanded report retains the official row,
  holder, raw status `Active`, normalized status `active`, source reference,
  source URL, and fetch time 2026-10-03 03:28:05 UTC. The P05 report also
  records the official DC corporate-name result and its file-number relationship.
- **Relationship, type, status:** The established DC registry entity's legal
  name, JM Plumbing & Heating Corporation, is the normalized exact holder
  name. The report records compatible trade evidence but no separate
  license-class label.
- **Why included:** The official board row and registry legal-name join support
  this P05 reference. It is counted as a legal-name-attributed recovery.

### TDLR:39935

- **Case(s):** P06. **Frozen identity:** FDL Electric. **Official holder:**
  FDL ELECTRIC.
- **Official licensing source:** Texas Department of Licensing and Regulation
  (TDLR) All Licenses dataset. **Record URL:**
  [official query by license number](https://data.texas.gov/resource/7358-krk7.json?license_number=39935&%24limit=100).
- **Retained evidence/date:** The expanded report retains the official row,
  source reference and URL, and fetch time 2026-10-03 03:28:40 UTC. It also
  retains first-party page evidence from [fdlelectric.com](https://fdlelectric.com/)
  with the text `TECL # 39935`.
- **Relationship, type, status:** Exact first-party license number and exact
  official canonical number agree; official holder name matches the business
  after normalization and trade/jurisdiction checks pass. The website uses
  `TECL`; the report does not retain a separate official status value.
- **Why included:** The first-party number and independent official TDLR row
  support the P06 reference.

### TDLR:441136

- **Evidence status:** OFFICIAL RECORD VERIFIED, RELATIONSHIP INSUFFICIENT.
- **Case(s):** P06. **Frozen identity:** FDL Electric. **Official record
  holder/business field:** DEVARD, CARL J.
- **Official licensing source:** TDLR All Licenses dataset. **Exact query:**
  [official query for license number 441136](https://data.texas.gov/resource/7358-krk7.json?license_number=441136&%24limit=100).
- **Retained evidence/date:** On 2026-10-03, the official exact-number query
  returned license `441136`, type `Master Electrician`, business name and
  owner name `DEVARD, CARL J`. The returned row contains no company association
  to FDL Electric. These returned fields and the query are retained here as a
  sanitized manual evidence note; no unnecessary contact details are copied.
- **Relationship, type, status:** The official record identifies an individual
  master electrician, not an FDL Electric business license. The existing
  first-party FDL evidence publishes `TECL # 39935`, not this number, and no
  qualifying principal relationship between the named holder and FDL Electric
  is retained. The official license record is verified; the frozen case link
  is not.
- **Why included / gap:** The exact official record exists, but the evidence
  does not independently establish that it belongs to the frozen P06 business.

### TSBPE:20628

- **Evidence status:** SOURCE INACCESSIBLE.
- **Case(s):** P08, P28. **Frozen identity:** Abacus Plumbing, Air Conditioning
  & Electrical. **Official holder/type/status:** not retained.
- **Official licensing source:** Texas State Board of Plumbing Examiners
  (TSBPE). **Source URL:** [official free licensee lists](https://tsbpe.texas.gov/free-licensee-list/).
- **Retained evidence/date:** The official TSBPE free-list page links the
  licensee lists, but the ordinary CSV request recorded by the expanded report
  returned HTTP 403; the public license portal attempt ended with an expired
  session. No workaround was attempted. No official row, type, status, or
  holder for this number was obtained. The existing Abacus first-party page
  evidence in the report publishes `M-20628` beside the name Alan O'Neill.
- **Relationship:** This is one frozen shared reference for P08/P28. The
  first-party clue is associated with an individual name; without the official
  board row and an established qualifying principal relationship, it does not
  independently establish the license-to-Abacus relationship.
- **Why included / gap:** The board record could not be independently checked
  because its public file path returned 403 and the search session expired.
  This is an access limitation, not a negative license finding.

### TDLR:30557

- **Case(s):** P08, P28. **Frozen identity:** Abacus Plumbing, Air Conditioning
  & Electrical. **Official holder:** ABACUS PLUMBING, AC, AND ELECTRICAL.
- **Official licensing source:** TDLR All Licenses dataset. **Record URL:**
  [official query by license number](https://data.texas.gov/resource/7358-krk7.json?license_number=30557&%24limit=100).
- **Retained evidence/date:** The expanded report retains the official row,
  source reference and URL, and fetch time 2026-10-03 03:29:02 UTC. It retains
  first-party page text `TECL 30557` from [abacusplumbing.com](https://www.abacusplumbing.com/).
- **Relationship, type, status:** First-party and official numbers match
  exactly; the holder is an exact normalized business-name match and trade and
  jurisdiction are compatible. A separate official status value is not
  retained.
- **Why included:** The first-party number, official TDLR row, and business-name
  agreement support this shared P08/P28 reference.

### TDLR:TACLA00135747C

- **Case(s):** P08, P28. **Frozen identity:** Abacus Plumbing, Air Conditioning
  & Electrical. **Official holder:** NEW ABACUS LLC.
- **Official licensing source:** TDLR All Licenses dataset. **Record URL:**
  [official query used by the report](https://data.texas.gov/resource/7358-krk7.json?license_number=135747&%24limit=100).
- **Retained evidence/date:** The expanded report retains the official
  canonical license number, holder, source reference and URL, and fetch time
  2026-10-03 03:29:02 UTC. It also retains first-party page text
  `TACLA135747C` from [abacusplumbing.com](https://www.abacusplumbing.com/).
- **Relationship, type, status:** The first-party number and official
  canonical record agree after the board's normalization. The official holder
  is a different legal name; the exact-number bridge records compatible board
  jurisdiction and trade. The report does not retain a separate status value.
- **Why included:** The exact first-party number and independently returned
  official TDLR row support this shared P08/P28 reference.

### TDLR:33423

- **Case(s):** P09. **Frozen identity:** Fox Service Company. **Official
  holder:** FOX SERVICE COMPANY.
- **Official licensing source:** TDLR All Licenses dataset. **Record URL:**
  [official name query retained in the report](https://data.texas.gov/resource/7358-krk7.json?%24q=Fox+Service+Company&%24limit=1000).
- **Retained evidence/date:** The expanded report retains the official row,
  source reference and URL, and fetch time 2026-10-03 03:29:05 UTC.
- **Relationship, type, status:** Exact normalized company name, exact
  official address, and compatible trade support the link. A different board
  phone is retained as audit evidence. The report does not retain a separate
  license status or class label.
- **Why included:** The official TDLR row and exact name/address agreement
  support the P09 reference.

### TDLR:TACLB00112806E

- **Evidence status:** VERIFIED AND RETAINED.
- **Case(s):** P09. **Frozen identity:** Fox Service Company. **Official
  holder/business field:** SA&H WESTERN HOLDINGS LLC.
- **Official licensing source:** TDLR All Licenses dataset. **Exact serial
  query:** [official query for TDLR serial 112806](https://data.texas.gov/resource/7358-krk7.json?license_number=112806&%24limit=100).
- **Retained evidence/date:** On 2026-10-03, the official exact-number query
  returned serial `112806`, type `A/C Contractor`, subtype `BE`, business
  `SA&H WESTERN HOLDINGS LLC`, and expiration 2027-06-10. TDLR's subtype
  encodes class B and suffix E, giving canonical
  number `TACLB00112806E`. The [Fox Service Company first-party site](https://www.foxservice.com/)
  publishes `HVAC: TACLB00112806E` and lists its Austin phone/address. These
  fields and the exact query are retained here as a sanitized manual evidence
  note; no credentials or session data were used or stored.
- **Relationship, type, status:** The first-party Fox page's exact HVAC
  license number canonicalizes to the same official TDLR number; the official
  row is a business/entity record, not an unrelated individual's record.
  TDLR's row does use a different legal business name (`SA&H WESTERN HOLDINGS
  LLC`), which is retained as audit evidence. The exact number published by
  Fox and independently returned by TDLR establishes the license disclosure
  relationship under the existing exact-number evidence rule. TDLR returned
  no status value in this query; the expiration date is transcribed above.
- **Why included:** The official TDLR row and Fox's exact first-party
  HVAC-license disclosure independently support this frozen reference.

### TSBPE:38471

- **Evidence status:** SOURCE INACCESSIBLE.
- **Case(s):** P09. **Frozen identity:** Fox Service Company. **Official
  holder/type/status:** not retained.
- **Official licensing source:** TSBPE. **Source URL:** [official free licensee
  lists](https://tsbpe.texas.gov/free-licensee-list/).
- **Retained evidence/date:** The official TSBPE CSV request in the expanded
  report returned HTTP 403; the public license portal attempt ended with an
  expired session. No workaround was attempted, and no official TSBPE row or
  type/status was obtained. The [Fox Service Company first-party site](https://www.foxservice.com/)
  explicitly publishes `Plumbing: 38471` alongside its HVAC and electrical
  license disclosures. That first-party disclosure is retained in the report,
  but it does not replace the unavailable official board record.
- **Relationship:** The first-party site links number 38471 to Fox's plumbing
  business, but the official board record and its holder are not independently
  verified. The license/business relationship therefore remains incomplete.
- **Why included / gap:** The official source's ordinary file request returned
  403 and the portal session expired; no anti-bot access control was bypassed.
  This is an access limitation, not a negative license finding.

### TSBPE:15007

- **Evidence status:** SOURCE INACCESSIBLE.
- **Case(s):** P10. **Frozen identity:** Benjamin Franklin Plumbing of Austin.
  **Official holder/type/status:** not retained.
- **Official licensing source:** TSBPE. **Source URL:** [official free
  licensee lists](https://tsbpe.texas.gov/free-licensee-list/).
- **Retained evidence/date:** The TSBPE ordinary CSV request recorded in the
  current expanded report returned HTTP 403; the public license portal attempt
  ended with an expired session. No workaround was attempted, and no official
  row or record-specific result was obtained. Places returned no identity
  candidate for P10, and no first-party exact-number disclosure is retained.
- **Relationship:** No official holder, type, status, first-party number,
  address, or record-specific business relationship is retained.
- **Why included / gap:** The official TSBPE record could not be retrieved
  through the ordinary public access path. This is an access limitation, not a
  negative license finding.

### TSBPE:45226

- **Evidence status:** SOURCE INACCESSIBLE.
- **Case(s):** P11. **Frozen identity:** Mr. Rooter Plumbing of Austin.
  **Official holder/type/status:** not retained.
- **Official licensing source:** TSBPE. **Source URL:** [official free
  licensee lists](https://tsbpe.texas.gov/free-licensee-list/).
- **Retained evidence/date:** The official TSBPE CSV request in the expanded
  report returned HTTP 403; the public license portal attempt ended with an
  expired session. No workaround was attempted. No official row, type/status,
  or first-party exact-number disclosure was retained for this number.
- **Relationship:** No record-specific holder or business relationship to the
  P11 identity is established in the retained material.
- **Why included / gap:** The official TSBPE record could not be retrieved
  through ordinary public access. This is an access limitation, not a negative
  license finding.

### CSLB:629538

- **Case(s):** P12, P13. **Frozen identity:** Cabrillo Plumbing, Heating & Air.
  **Official holder:** CABRILLO PLUMBING & HEATING.
- **Official licensing source:** California Contractors State License Board
  (CSLB) classification download. **Source URL:**
  [official CSLB classification data](https://web.cslb.ca.gov/Onlineservices/DataPortal/ListByClassification).
- **Retained evidence/date:** The expanded report retains the official row,
  holder, raw status `CLEAR`, source reference and URL, and fetch time
  2026-10-03 03:29:13 UTC. It retains the [Cabrillo first-party page](https://discovercabrillo.com/)
  publishing the exact input phone and matching official number.
- **Relationship, type, status:** Exact first-party number and official
  canonical number agree; the official holder's business name, website evidence,
  and address/trade checks support P13. The report leaves normalized status
  unset. P12 and P13 share one globally deduplicated license; P12 itself did
  not resolve to a Places identity.
- **Why included:** Official CSLB data and exact first-party corroboration
  support the shared P12/P13 reference.

### CSLB:1061107

- **Evidence status:** VERIFIED AND RETAINED.
- **Case(s):** P14. **Frozen identity:** TRIO Heating, Air & Plumbing.
- **Official licensing source:** California Contractors State License Board
  (CSLB), public license posting. **Record source:** [official CSLB posting
  PL251107](https://www.cslb.ca.gov/Resources/CSLB/PL251107.pdf).
- **Retained evidence/date:** The official CSLB posting dated 2025-11-07
  lists `TRIO HEATING & AIR`, license `1061107`, address `1310 N 4TH STREET,
  SAN JOSE, CA 95112`, and classes `C20` and `B`. On 2026-10-03, the
  [TRIO first-party contact page](https://trioheatingandair.com/contact/)
  displayed `Lic: #1061107`, phone `(415) 226-4125`, and main office `1310 N
  4th St, San Jose, CA 95112`. The source fields and comparison are retained
  here as a sanitized manual evidence note. The contact form's CAPTCHA was
  not interacted with.
- **Relationship, type, status:** The official contractor name/class/address
  and first-party exact number plus matching office address support the frozen
  TRIO identity. CSLB lists classifications C20 and B in the posting. The
  posting is a dated official source record; no current status is asserted from
  that posting alone.
- **Why included:** The CSLB record and the exact first-party number/address
  establish the P14 business/license relationship.

### CSLB:1103922

- **Case(s):** P15. **Frozen identity:** Prime Plumbing and Drain, Inc.
  **Official holder:** PRIME PLUMBING AND DRAIN INC.
- **Official licensing source:** CSLB License Master CSV. **Record URL:**
  [official CSLB License Master data](https://web.cslb.ca.gov/OnlineServices/DataPortal/DownLoadFile.ashx?fName=MasterLicenseData&type=C).
- **Retained evidence/date:** The expanded report retains the official row,
  holder, raw status `CLEAR`, source reference and URL, and fetch time
  2026-10-03 03:30:53 UTC.
- **Relationship, type, status:** Exact normalized business name, input phone,
  and address support the business relationship. Raw CSLB status is `CLEAR`;
  normalized status is not set. A separate classification label is not
  retained in the accepted record.
- **Why included:** The official CSLB row and exact business identity evidence
  support the P15 reference.

### CSLB:806952

- **Case(s):** P16. **Frozen identity:** Roto-Rooter San Francisco. **Official
  holder:** NUROTOCO.
- **Official licensing source:** CSLB classification download. **Source URL:**
  [official CSLB classification data](https://web.cslb.ca.gov/Onlineservices/DataPortal/ListByClassification).
- **Retained evidence/date:** The expanded report retains the official row,
  holder, raw status `CLEAR`, source reference and URL, and fetch time
  2026-10-03 03:31:37 UTC. The report also retains the [first-party San
  Francisco page](https://www.rotorooter.com/sanfrancisco/) with the exact
  input phone, business name/address evidence, and published license number.
- **Relationship, type, status:** The verified first-party page and CSLB data
  publish the same exact number; the official record is a business/entity
  candidate and trade/jurisdiction safeguards passed. Raw status is `CLEAR`;
  normalized status is not set. The evaluator separately marks the predicted
  Places label `Roto-Rooter Plumbing & Water Cleanup` incorrect against the
  frozen `Roto-Rooter San Francisco` identity. License-number evidence does not
  change that identity-label result.
- **Why included:** The first-party exact-number evidence and independent CSLB
  row support the P16 license reference, while the identity-label discrepancy
  remains disclosed.

## Evidence closure summary

| Measure | Count |
|---|---:|
| Frozen references with sufficient retained primary evidence | 16 / 23 |
| Still insufficient or inaccessible | 7 / 23 |

The seven unresolved references are DPOR:2710061674, MD_ELECTRICIANS:14003,
TDLR:441136, TSBPE:20628, TSBPE:38471, TSBPE:15007, and TSBPE:45226. The
inaccessible sources were not bypassed, and none of the unresolved references
is treated as a confirmed negative.
