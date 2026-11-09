"""Build a JSONL fine-tune set for constrained JSON extraction of Indian vernacular money phrases (for Evon/any small LLM).

Synthetic only: templates x amounts x generic names (no real people), Hinglish-Roman and Devanagari. Labels come from construction,
then each row is VERIFIED by Munim's own deterministic extractor (intent + amount must agree) so the set never teaches a wrong
amount. Deterministic seed; 10 % held out. Usage: python scripts/make_finetune_set.py out_dir [n_rows]
"""
from __future__ import annotations

import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from munim.scribe.extract import extract                      # noqa: E402
from munim.scribe.indic_numbers import rupees_to_words        # noqa: E402
from munim.scribe.names import Customer                       # noqa: E402

NAMES = ["Sharma", "Verma", "Gupta", "Yadav", "Singh", "Mishra", "Pandey", "Khan", "Joshi", "Patel", "Iyer", "Reddy", "Das", "Roy"]
ROMAN = {"add_udhaar": ["{n} ji ko {a} udhaar likh do", "{n} ko {a} ka udhaar", "{n} ji ne {a} ka saaman liya, udhaar likho", "{a} udhaar {n} ke naam"],
         "record_payment": ["{n} ji ne {a} diye", "{n} ne {a} jama kiye", "{n} ji se {a} mile", "{n} ne {a} chuka diye"]}
DEVA = {"add_udhaar": ["{n} जी को {a} उधार लिख दो", "{n} को {a} का उधार"],
        "record_payment": ["{n} जी ने {a} दिए", "{n} ने {a} जमा किए"]}
DEVA_NAMES = {"Sharma": "शर्मा", "Verma": "वर्मा", "Gupta": "गुप्ता", "Yadav": "यादव", "Singh": "सिंह", "Mishra": "मिश्रा", "Pandey": "पांडे",
              "Khan": "खान", "Joshi": "जोशी", "Patel": "पटेल", "Iyer": "अय्यर", "Reddy": "रेड्डी", "Das": "दास", "Roy": "रॉय"}
AMOUNTS = [20, 30, 50, 60, 75, 80, 100, 120, 150, 200, 250, 300, 450, 500, 600, 750, 900, 1000, 1200, 1500, 2000, 2500, 3000, 5000]
SYSTEM = "Extract JSON {intent, customer, amount_paise} from an Indian shopkeeper's ledger sentence. intent is add_udhaar or record_payment. Amounts as integer paise. No extra text."


def build(n: int, seed: int = 7):
    rnd = random.Random(seed)
    custs = [Customer(i + 1, nm, []) for i, nm in enumerate(NAMES)]
    rows, tries, bad = [], 0, 0
    while len(rows) < n and tries < n * 5:
        tries += 1
        intent = rnd.choice(list(ROMAN))
        nm, amt = rnd.choice(NAMES), rnd.choice(AMOUNTS)
        deva = rnd.random() < 0.3
        words = rupees_to_words(amt, "deva" if deva else "roman") if rnd.random() < 0.5 else str(amt)
        tpl = rnd.choice((DEVA if deva else ROMAN)[intent])
        text = tpl.format(n=DEVA_NAMES[nm] if deva else nm, a=words + (" रुपये" if deva and words.isdigit() else ""))
        ex = extract(text, custs)
        if ex.intent != intent or ex.amount_paise != amt * 100 or ex.customer is None or ex.customer.name != nm:
            bad += 1
            continue
        rows.append({"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text},
                                  {"role": "assistant", "content": json.dumps({"intent": intent, "customer": nm, "amount_paise": amt * 100}, ensure_ascii=False)}]})
    return rows, bad, tries


def main(argv: list[str]) -> None:
    out = argv[1] if len(argv) > 1 else "finetune_out"
    n = int(argv[2]) if len(argv) > 2 else 2400
    rows, bad, tries = build(n)
    os.makedirs(out, exist_ok=True)
    cut = int(len(rows) * 0.9)
    for name, part in (("train.jsonl", rows[:cut]), ("heldout.jsonl", rows[cut:])):
        with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
            for r in part:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"rows={len(rows)} train={cut} heldout={len(rows) - cut} rejected_by_extractor={bad}/{tries}")


if __name__ == "__main__":
    main(sys.argv)
