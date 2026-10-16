"""One-off probe: how are senses structured for a few sample headwords?"""

import sys
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings

TEI_NS = "{http://www.tei-c.org/ns/1.0}"
WANTED = {"Kind", "Wolf"}

path = get_settings().dict_tei_path
found = 0
for event, element in ElementTree.iterparse(path, events=("end",)):
    if element.tag != f"{TEI_NS}entry":
        continue
    orth = element.find(f"{TEI_NS}form/{TEI_NS}orth")
    name = "".join(orth.itertext()).strip() if orth is not None else ""
    if name in WANTED:
        senses = element.findall(f"{TEI_NS}sense")
        print(f"=== {name} (id={element.get('xml:id')}) senses={len(senses)} ===")
        for i, sense in enumerate(senses):
            cits = sense.findall(f"{TEI_NS}cit[@type='trans']")
            print(f"  sense[{i}] cits={len(cits)}")
            for cit in cits[:12]:
                quote = cit.find(f"{TEI_NS}quote")
                extra = [child.tag.replace(TEI_NS, "") for child in cit]
                text = "".join(quote.itertext()).strip() if quote is not None else ""
                print(f"    - {text!r}  children={extra}")
        found += 1
        if found == len(WANTED):
            break
    element.clear()
