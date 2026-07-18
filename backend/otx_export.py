"""Export a commissioning program as an OTX-style XML procedure.

OTX (Open Test sequence eXchange format, ISO 13209) is the real industry
standard for exchanging executable diagnostic test/commissioning sequences
between tools, independent of any single vendor's tester software. The JD's
keyword list explicitly names service-oriented vehicle diagnostics, and a
program that only ever leaves this system as ad-hoc JSON is a dead end in a
real plant - a tester needs something it can load and execute.

This module does NOT claim to be a certified, schema-complete OTX document -
that spec runs to hundreds of pages covering data types, flow control,
declarations, and signature handling this prototype has no need to model.
What it produces is a structurally faithful subset: an OTX <procedures>
document with one <procedure>, whose <flow> is an ordered sequence of
<diag-comm-action> elements (one per commissioning step) carrying the ECU
reference, the UDS service id, and the step's declared dependencies as
<precondition> references - which is precisely the information a real OTX
runtime would need to sequence execution correctly.
"""

from __future__ import annotations

from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom

from .models import CommissioningProgram


def to_otx_xml(program: CommissioningProgram) -> str:
    """Render a CommissioningProgram as an OTX-style XML procedure document."""

    root = Element(
        "otx",
        {
            "xmlns": "http://www.asam.net/OTX/1.0.0",
            "xmlns:spec2program": "https://github.com/iamvisheshsrivastava/spec2program",
            "spec2program:generatedBy": "spec2program",
            "spec2program:note": (
                "Illustrative OTX-style export, not a certified ISO 13209 document."
            ),
        },
    )
    procedure = SubElement(
        root,
        "procedure",
        {"id": f"proc_{program.vehicle_id}", "name": f"Commission {program.vehicle_id}"},
    )
    if program.notes:
        notes_el = SubElement(procedure, "description")
        notes_el.text = program.notes

    flow = SubElement(procedure, "flow")

    for step in program.steps:
        action = SubElement(
            flow,
            "diag-comm-action",
            {
                "id": f"step_{step.order}",
                "order": str(step.order),
                "type": step.step_type.value,
            },
        )
        SubElement(action, "target-ecu").text = step.ecu_id
        if step.uds_service:
            SubElement(action, "diag-service").text = step.uds_service
        SubElement(action, "short-description").text = step.description
        SubElement(action, "expected-duration-seconds").text = str(step.estimated_seconds)

        if step.depends_on:
            preconditions = SubElement(action, "preconditions")
            for dep in step.depends_on:
                SubElement(preconditions, "precondition", {"ref": f"step_{dep}"})

    # Pretty-print for a document a human is meant to be able to read and
    # load into another tool, not just machine-parse.
    rough = tostring(root, encoding="unicode")
    return minidom.parseString(rough).toprettyxml(indent="  ")
