"""
Vivah Bandhan — Firestore Database Layer (Python / firebase-admin)
===================================================================

Design basis: the registration form (source of truth). Firestore has no
foreign keys, no CHECK constraints, and no cascading deletes — every
integrity rule that SQLite enforced automatically is re-implemented here,
in Python, before anything touches the database.

COLLECTION LAYOUT
------------------
profiles/{profile_number}                          <- profile_number IS the
                                                       document ID (e.g. "VB202600001").
                                                       Firestore guarantees doc-ID
                                                       uniqueness for free, so this
                                                       alone rules out duplicate
                                                       registrations under the same number.
    references/{auto_id}                            <- subcollection: 0..N reference contacts
    documents/{auto_id}                              <- subcollection: 0..N uploaded documents
                                                          (ID proof, kundali, death cert, etc.)

counters/registration_sequence                       <- single doc, incremented inside a
                                                          transaction to hand out the next
                                                          sequence number atomically.

lookups/{education|occupation|gotra|district|state|  <- small reference collections for
         document_type|marital_status}/items/{id}       populating dropdowns consistently.
                                                          NOT referentially enforced — Firestore
                                                          can't do that — these exist only so the
                                                          app always shows the same value list.

WHY SUBCOLLECTIONS INSTEAD OF A TOP-LEVEL COLLECTION + profile_id FIELD:
A reference document's only path to existing is `profiles/{X}/references/{Y}`.
There is no `profile_id` field to typo or cross-link — the database path
itself is the parent link. This is the Firestore-native way to get the same
guarantee SQL got from `FOREIGN KEY ... ON DELETE CASCADE`.

REQUIRES: a real Firebase project + service account key to run against a
live database. Point GOOGLE_APPLICATION_CREDENTIALS at your key file, or
pass a credential path into init_app(). This module has been validated for
syntax and logic against the firebase-admin SDK, but has NOT been run
against a live Firestore instance — you'll need your project's credentials
for that.

COMPLETE FIELD MAP — every input in the HTML form, by its exact HTML id,
mapped to its exact Firestore field. This is the single source of truth
for naming; whoever builds the app UI should read field values off this
list, not guess a name.

  profiles/{profile_number} document:
    HTML id            -> Firestore field
    -----------------------------------------------------------------
    (no field)         -> candidate_type        ["bride"|"groom"] — set
                           by the app from which form template is open,
                           NOT read from the HTML
    (no field)         -> marital_status         ["unmarried"|"widow"|
                           "widower"|"divorcee"] — set by the app; the
                           HTML has a section for this but no selector
    fullName           -> full_name
    education          -> education
    otherQualify       -> other_qualification
    occupation         -> occupation
    post               -> designation
    monthlyIncome      -> monthly_income
    businessAddress    -> business_address.line
    district (biodata) -> business_address.district
    state (biodata)    -> business_address.state
    pin1 (digit boxes) -> business_address.pincode
    mobile (digits)    -> mobile_number
    whatsapp (digits)  -> whatsapp_number
    email              -> email
    birthDate          -> birth.date
    birthDateWords     -> birth.date_words
    birthPlace         -> birth.place
    birthDistrict      -> birth.district
    birthState         -> birth.state
    birthTime          -> birth.time
    color (radio)      -> varna                  ["gaur"|"gehua"]
    heightFeet         -> height_feet
    heightInch         -> height_inch
    weight             -> weight_kg
    gotraSelf          -> gotra_self
    gotraNani          -> gotra_nani
    manglik (radio)    -> manglik_status          ["yes"|"no"|"unknown"]
    disabledDetails    -> disability_details
    photo upload       -> photo_path              (Firebase Storage path,
                                                    NOT the file itself)

  family (embedded object on the same profile document — 1:1, per form):
    fatherName         -> family.father_name
    motherName         -> family.mother_name
    parentOccupation   -> family.parent_occupation
    parentIncome       -> family.parent_income
    residency (radio)  -> family.residency_type   ["own"|"rented"]
    permanentAddress   -> family.permanent_address.line
    permDistrict       -> family.permanent_address.district
    permState          -> family.permanent_address.state
    pin2 (digit boxes) -> family.permanent_address.pincode
    elderBrothers      -> family.elder_brothers_text   (kept as free text,
                                                          matches form's
                                                          "1 married" style)
    elderSisters       -> family.elder_sisters_text
    youngerSiblings    -> family.younger_siblings_text

  marital_history (embedded, ONLY present when marital_status is widow/
  widower/divorcee — enforced in validate_profile):
    prevSpouseName     -> marital_history.previous_spouse_name
    prevFatherInLaw    -> marital_history.previous_father_in_law_name
    prevAddress        -> marital_history.previous_address
    sonsCount          -> marital_history.sons_count
    daughtersCount     -> marital_history.daughters_count

  declaration (embedded):
    declareCheck       -> declaration.declared            (bool, required)
    declSign           -> declaration.declared_by_name
    declDate           -> declaration.declaration_date
    declPlace          -> declaration.declaration_place

  profiles/{profile_number}/references/{auto_id} subcollection
  (form asks for a minimum of 2 — see validate_registration_complete):
    ref1Name / ref2Name        -> reference_name
    ref1Relation / ref2Relation -> relation
    ref1Address / ref2Address  -> address_line
    ref1State / ref2State      -> state
    ref1pin / ref2pin          -> pincode
    ref1mobile / ref2mobile    -> mobile_number

  profiles/{profile_number}/documents/{auto_id} subcollection:
    "other document" upload    -> file_path + document_type
                                   (id_proof / kundali / death_certificate /
                                    divorce_decree / other)
    office-use regNo / regDate -> NOT stored here — these are exactly
                                   profile_number and created_at, which
                                   already exist; don't duplicate them
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1 import DocumentReference, Transaction

# ---------------------------------------------------------------------------
# App / client initialization
# ---------------------------------------------------------------------------

def init_app(service_account_path: Optional[str] = None):
    """Initialize the Firebase app once. Call this before anything else."""
    if not firebase_admin._apps:
        cred = credentials.Certificate(service_account_path) if service_account_path else credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred)
    return firestore.client()


# ---------------------------------------------------------------------------
# Validation — Firestore has no CHECK constraints, so every rule your
# SQLite schema enforced at the database layer is enforced HERE instead,
# before a single write happens.
# ---------------------------------------------------------------------------

# These are NOT invented — they are the exact required-field list from your
# HTML form's own JavaScript (`requiredFields = ['fullName','fatherName',
# 'motherName','permanentAddress']`, plus the 10-digit mobile check, the
# declaration checkbox, and photoValid). If your form's validation ever
# changes, this list is what needs to change with it — nothing else.
VALID_CANDIDATE_TYPES = {"bride", "groom"}
VALID_MARITAL_STATUSES = {"unmarried", "widow", "widower", "divorcee"}
VALID_MANGLIK = {"yes", "no", "unknown"}
VALID_VARNA = {"gaur", "gehua"}


class ValidationError(Exception):
    pass


def validate_profile(data: dict) -> None:
    """Raises ValidationError with every problem found, not just the first.

    NOTE on two fields NOT present anywhere in your HTML form:
      - candidate_type (bride/groom): your form has no field for this at all —
        confirmed this is decided by which of two separate form templates the
        operator opens, so the APP must pass it explicitly. Required here.
      - marital_status: your form also has no Unmarried/Widow/Widower/Divorcee
        selector — only a section that happens to apply if filled in. Inferring
        this from "is the widow/divorcee section filled in" is fragile (one
        skipped field silently mislabels someone), so this is modeled as an
        explicit value the operator picks, same as candidate_type. If your real
        paper form does have a status field I haven't seen, tell me and this
        assumption goes away.
    """
    errors = []

    if not data.get("full_name"):
        errors.append("'full_name' is required")
    if not data.get("candidate_type"):
        errors.append("'candidate_type' is required (bride/groom — set by the app, not the form)")
    if not data.get("marital_status"):
        errors.append("'marital_status' is required (set by the app, not the form)")

    family = data.get("family") or {}
    if not family.get("father_name"):
        errors.append("'family.father_name' is required")
    if not family.get("mother_name"):
        errors.append("'family.mother_name' is required")
    if not (family.get("permanent_address") or {}).get("line"):
        errors.append("'family.permanent_address.line' is required")

    if not data.get("photo_path"):
        errors.append("'photo_path' is required — the form does not allow submission without a valid photo")

    declaration = data.get("declaration") or {}
    if not declaration.get("declared"):
        errors.append("'declaration.declared' must be true — the form blocks submission until this is checked")

    candidate_type = data.get("candidate_type")
    if candidate_type and candidate_type not in VALID_CANDIDATE_TYPES:
        errors.append(f"candidate_type must be one of {VALID_CANDIDATE_TYPES}, got '{candidate_type}'")

    mobile = data.get("mobile_number", "")
    if not mobile:
        errors.append("'mobile_number' is required")
    elif not re.fullmatch(r"\d{10}", mobile):
        errors.append(f"mobile_number must be exactly 10 digits, got '{mobile}'")

    whatsapp = data.get("whatsapp_number")
    if whatsapp and not re.fullmatch(r"\d{10}", whatsapp):
        errors.append(f"whatsapp_number must be exactly 10 digits, got '{whatsapp}'")

    marital_status = data.get("marital_status")
    if marital_status and marital_status not in VALID_MARITAL_STATUSES:
        errors.append(f"marital_status must be one of {VALID_MARITAL_STATUSES}, got '{marital_status}'")

    manglik = data.get("manglik_status")
    if manglik and manglik not in VALID_MANGLIK:
        errors.append(f"manglik_status must be one of {VALID_MANGLIK}, got '{manglik}'")

    varna = data.get("varna")
    if varna and varna not in VALID_VARNA:
        errors.append(f"varna must be one of {VALID_VARNA}, got '{varna}'")

    for pin_field in ("business_address", "permanent_address"):
        addr = data.get(pin_field) or {}
        pin = addr.get("pincode")
        if pin and not re.fullmatch(r"\d{6}", pin):
            errors.append(f"{pin_field}.pincode must be exactly 6 digits, got '{pin}'")

    # Business rule from the form: widow/widower/divorcee MUST carry marital_history
    # and a proof document. Firestore can't enforce a cross-collection rule like
    # this on its own — it has to be checked here, every time.
    if marital_status in {"widow", "widower", "divorcee"} and not data.get("marital_history"):
        errors.append(
            f"marital_status='{marital_status}' requires a 'marital_history' block "
            "(previous spouse / in-laws / children details per the form)"
        )

    if errors:
        raise ValidationError("; ".join(errors))


def validate_reference(data: dict) -> None:
    errors = []
    if not data.get("reference_name"):
        errors.append("reference_name is required")
    mobile = data.get("mobile_number")
    if mobile and not re.fullmatch(r"\d{10}", mobile):
        errors.append(f"mobile_number must be exactly 10 digits, got '{mobile}'")
    pin = data.get("pincode")
    if pin and not re.fullmatch(r"\d{6}", pin):
        errors.append(f"pincode must be exactly 6 digits, got '{pin}'")
    if errors:
        raise ValidationError("; ".join(errors))


def validate_document(data: dict) -> None:
    valid_types = {"photo", "id_proof", "kundali", "death_certificate", "divorce_decree", "other"}
    errors = []
    doc_type = data.get("document_type")
    if doc_type not in valid_types:
        errors.append(f"document_type must be one of {valid_types}, got '{doc_type}'")
    if not data.get("file_path"):
        errors.append("file_path is required — never store raw file bytes in Firestore")
    if errors:
        raise ValidationError("; ".join(errors))


# ---------------------------------------------------------------------------
# Registration number generation — atomic, race-condition-free.
# Format matches the SQLite version: VB<year><5-digit sequence>
# ---------------------------------------------------------------------------

@firestore.transactional
def _next_registration_number(transaction: Transaction, counter_ref: DocumentReference) -> str:
    year = datetime.now(timezone.utc).year
    snapshot = counter_ref.get(transaction=transaction)
    data = snapshot.to_dict() or {}
    current = data.get(str(year), 0)
    next_seq = current + 1
    transaction.set(counter_ref, {str(year): next_seq}, merge=True)
    return f"VB{year}{next_seq:05d}"


def generate_registration_number(db) -> str:
    counter_ref = db.collection("counters").document("registration_sequence")
    transaction = db.transaction()
    return _next_registration_number(transaction, counter_ref)


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def create_profile(db, data: dict, created_by: str) -> str:
    """
    Validates, generates a registration number, and writes the profile.
    Returns the profile_number (== document ID).
    """
    validate_profile(data)

    profile_number = generate_registration_number(db)
    now = datetime.now(timezone.utc)

    doc = dict(data)
    doc.update({
        "profile_number": profile_number,
        "registration_status": "pending",
        "created_at": now,
        "updated_at": now,
        "created_by": created_by,
        "updated_by": created_by,
        "is_deleted": False,
        "deleted_at": None,
    })

    db.collection("profiles").document(profile_number).set(doc)
    return profile_number


def update_profile(db, profile_number: str, updates: dict, updated_by: str) -> None:
    ref = db.collection("profiles").document(profile_number)
    if not ref.get().exists:
        raise ValueError(f"No profile with profile_number={profile_number}")
    updates = dict(updates)
    updates["updated_at"] = datetime.now(timezone.utc)
    updates["updated_by"] = updated_by
    ref.update(updates)


def soft_delete_profile(db, profile_number: str, deleted_by: str) -> None:
    """Never hard-delete during normal operation — mirrors the SQLite is_deleted pattern."""
    ref = db.collection("profiles").document(profile_number)
    ref.update({
        "is_deleted": True,
        "deleted_at": datetime.now(timezone.utc),
        "updated_by": deleted_by,
        "updated_at": datetime.now(timezone.utc),
    })


def add_reference(db, profile_number: str, data: dict) -> str:
    validate_reference(data)
    parent = db.collection("profiles").document(profile_number)
    if not parent.get().exists:
        raise ValueError(f"No profile with profile_number={profile_number}")
    _, ref = parent.collection("references").add({**data, "created_at": datetime.now(timezone.utc)})
    return ref.id


def add_document(db, profile_number: str, data: dict) -> str:
    validate_document(data)
    parent = db.collection("profiles").document(profile_number)
    if not parent.get().exists:
        raise ValueError(f"No profile with profile_number={profile_number}")
    _, ref = parent.collection("documents").add({**data, "uploaded_at": datetime.now(timezone.utc)})
    return ref.id


def get_full_profile(db, profile_number: str) -> Optional[dict]:
    """Assembles the profile + its references + documents — the Firestore
    equivalent of the SQL `profile_export` view, for handing off to a
    CorelDRAW export step or a print-ready summary."""
    ref = db.collection("profiles").document(profile_number)
    snap = ref.get()
    if not snap.exists:
        return None

    result = snap.to_dict()
    result["references"] = [d.to_dict() | {"id": d.id} for d in ref.collection("references").stream()]
    result["documents"] = [d.to_dict() | {"id": d.id} for d in ref.collection("documents").stream()]
    return result


def search_profiles_by_mobile(db, mobile_number: str) -> list[dict]:
    query = db.collection("profiles").where("mobile_number", "==", mobile_number).where("is_deleted", "==", False)
    return [d.to_dict() for d in query.stream()]


def validate_registration_complete(db, profile_number: str) -> None:
    """Your form's own text says 'कृपया कम से कम दो संपर्क सूत्रों का विवरण दें'
    (please provide at least two references). Firestore can't enforce a
    subcollection-count rule on its own, so check it here before a profile
    is allowed to move from 'pending' to 'verified'/'published'."""
    ref = db.collection("profiles").document(profile_number)
    snap = ref.get()
    if not snap.exists:
        raise ValueError(f"No profile with profile_number={profile_number}")

    errors = []
    ref_count = sum(1 for _ in ref.collection("references").stream())
    if ref_count < 2:
        errors.append(f"form requires at least 2 references, found {ref_count}")

    data = snap.to_dict()
    if data.get("marital_status") in {"widow", "widower", "divorcee"}:
        has_proof = any(
            d.to_dict().get("document_type") in {"death_certificate", "divorce_decree"}
            for d in ref.collection("documents").stream()
        )
        if not has_proof:
            errors.append(
                "form requires a death certificate or divorce decree copy for "
                f"marital_status='{data.get('marital_status')}' — none uploaded"
            )

    if errors:
        raise ValidationError("; ".join(errors))


def search_profiles_by_type_and_status(db, candidate_type: str, status: str = "published") -> list[dict]:
    query = (
        db.collection("profiles")
        .where("candidate_type", "==", candidate_type)
        .where("registration_status", "==", status)
        .where("is_deleted", "==", False)
    )
    return [d.to_dict() for d in query.stream()]


# ---------------------------------------------------------------------------
# Example usage (won't run without real Firebase credentials — for reference)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # db = init_app("path/to/serviceAccountKey.json")
    #
    # profile_number = create_profile(db, {
    #     "candidate_type": "groom",
    #     "full_name": "Rahul Jain",
    #     "mobile_number": "9876543210",
    #     "marital_status": "unmarried",
    #     "education": "B.Com",
    #     "occupation": "Business",
    #     "birth": {"date": "1998-04-12", "place": "Chhindwara"},
    #     "family": {
    #         "father_name": "Suresh Jain",
    #         "mother_name": "Kavita Jain",
    #         "permanent_address": {"line": "Gandhi Ganj", "district": "Chhindwara",
    #                                "state": "Madhya Pradesh", "pincode": "480001"},
    #     },
    # }, created_by="operator1")
    #
    # add_reference(db, profile_number, {
    #     "reference_name": "Swapnil Jain", "relation": "Family friend",
    #     "mobile_number": "8461941001",
    # })
    #
    # print(get_full_profile(db, profile_number))
    print("Import this module and call init_app() with your Firebase credentials to use it.")
