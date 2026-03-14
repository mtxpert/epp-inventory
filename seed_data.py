"""Seed database with initial component and kit data from spreadsheet."""
from models import db, Component, Kit, KitComponent

COMPONENTS = {
    "pipes": {
        "HP-NMD": {"name": "Noisemaker Delete Pipe (3\"-2.5\")", "old_pn": "ECO-250-01", "qty": 55},
        "HP-SHRT": {"name": "Hot Pipe 1 - L Pipe (Short)", "old_pn": "ECO-200-04", "qty": 20},
        "HP-LNG": {"name": "Hot Pipe 2 - BOV Pipe (Long)", "old_pn": "ECO-200-03", "qty": 16},
        "IN-HEAT": {"name": "Intake Heat Shield", "old_pn": "", "qty": 27},
        "IN-S": {"name": "Intake 1 - Question Mark Pipe", "old_pn": "EPP-TB-01", "qty": 33},
        "IN-90": {"name": "Intake 2 - L Pipe (90)", "old_pn": "EPP-TB-02", "qty": 27},
        "IN-STK": {"name": "Intake 3 - Stock Hose Version", "old_pn": "EPP-TB-03-R1", "qty": 18},
        "IN-CUST": {"name": "Intake 4 - Custom Hose Version", "old_pn": "EPP-TB-03", "qty": 34},
        "TR-I-BG": {"name": "F150 Intake Pipe 1", "old_pn": "PTBC-ECO-P400-01", "qty": 1},
        "TR-I-LNG": {"name": "F150 Intake Pipe 2 (Long)", "old_pn": "PTBC-ECO-P300-02", "qty": 1},
        "FU-I-PRT": {"name": "Fusion Intake - Curved Pipe", "old_pn": "AA-713-01", "qty": 45},
        "FU-I-RCIRC": {"name": "Fusion Intake - Long Pipe", "old_pn": "AA-713-03", "qty": 39},
        "FU-I-PLN": {"name": "Fusion Intake - Short Pipe", "old_pn": "RTI-02", "qty": 41},
        "FU-C-LNG": {"name": "Fusion Charge - Long Pipe", "old_pn": "RTC-02", "qty": 61},
        "FU-C-MID90": {"name": "Fusion Charge - Medium 90 Pipe", "old_pn": "FTC-04", "qty": 55},
        "FU-C-SML": {"name": "Fusion Charge - Small Pipe", "old_pn": "RTC-03", "qty": 54},
        "FU-C-MCRO": {"name": "Fusion Charge - Micro Pipe", "old_pn": "RTC-01", "qty": 61},
        "FU-C-TBDY": {"name": "Fusion Charge - TB Pipe (2.5\")", "old_pn": "AA-713-02", "qty": 55},
    },
    "couplers": {
        "SH-0-51": {"name": "2\" Hump Hose", "qty": 227},
        "SH-0-63": {"name": "2.5\" Hump Hose", "qty": 142},
        "SH-0-76": {"name": "3\" Hump Hose", "qty": 122},
        "SR-0-38-51": {"name": "1.5\"-2\" Transition Reducer", "qty": 146},
        "SR-0-45-51": {"name": "F150 Transition 1.75\"-2\"", "qty": 116},
        "SR-0-45-63": {"name": "F150 Transition 1.75\"-2.5\"", "qty": 91},
        "SR-0-48-51": {"name": "Fusion Transition (Charge Pipe)", "qty": 110},
        "SR-0-63-76": {"name": "Fusion Transition (Intake)", "qty": 148},
        "SS-0-51": {"name": "2\" Straight Hose", "qty": 232},
        "SS-0-63": {"name": "2.5\" Straight Hose", "qty": 64},
        "SRE-90-45-63": {"name": "SHO Front Turbo Hose (90deg)", "qty": 140},
        "SRE-90-51-63": {"name": "Fusion Intake Hose (90deg)", "qty": 80},
        "SRE-90-63-70": {"name": "Fusion Charge Hose (90deg)", "qty": 67},
        "XH-CP023": {"name": "SHO Custom Intake Hose", "qty": 47},
        "XH-CP024": {"name": "SHO Custom Charge Hose", "qty": 23},
        "EXP-45-63": {"name": "Explorer Sport 2.5\" 45deg Hose", "qty": 26},
    },
    "clamps": {
        "CLAMP-150": {"name": "1.5\" T-Bolt Clamp (Mishimoto 175)", "qty": 33},
        "CLAMP-175": {"name": "1.75\" T-Bolt Clamp (Mishimoto 200)", "qty": 80},
        "CLAMP-200": {"name": "2\" T-Bolt Clamp (Mishimoto 225)", "qty": -1},
        "CLAMP-250": {"name": "2.5\" T-Bolt Clamp (Mishimoto 275)", "qty": 7},
        "CLAMP-275": {"name": "2.75\" T-Bolt Clamp (Mishimoto 300)", "qty": 149},
        "CLAMP-300": {"name": "3\" T-Bolt Clamp (Mishimoto 325)", "qty": 16},
    },
    "misc": {
        "FILTER": {"name": "Cold Air Intake Filter", "qty": 95},
        "STICKER": {"name": "EPP Sticker", "qty": 369},
        "NMD-SCREW": {"name": "NMD Screws (set)", "qty": 216},
        "NPT-125": {"name": "1/8\" NPT Bung", "qty": 10},
        "NPT-250": {"name": "1/4\" NPT Bung", "qty": 27},
        "NPT-375": {"name": "3/8\" NPT Bung", "qty": 2},
        "MAP-SHO": {"name": "SHO MAP Sensor Mount", "qty": 0},
        "BOV-SHO": {"name": "SHO BOV Mount", "qty": 30},
        "BOV-FUSION": {"name": "Fusion BOV Mount", "qty": 69},
        "BOV-TIAL": {"name": "Tial BOV Mount", "qty": 8},
        "BOV-XS": {"name": "TurboXS BOV Mount", "qty": 7},
        "BOV-HKS": {"name": "HKS BOV Mount", "qty": 2},
        "VAC-PORT": {"name": "Vacuum Port (catch can)", "qty": 4},
        "BARB-075": {"name": "3/4\" Barb", "qty": 16},
        "BOX-28": {"name": "28x16x7 Shipping Box", "qty": 14},
        "BOX-24S": {"name": "24x12x6 Shipping Box", "qty": 21},
        "BOX-24L": {"name": "24x14x10 Shipping Box", "qty": 21},
        "TAPE": {"name": "Packing Tape", "qty": 10},
    },
    "raptor": {
        # Connector housings (ordered from Mouser, shipped to Sean @ Innova Speed for assembly)
        "RAPT-CON-LSW": {"name": "Left Switch Connector (34824-0124)", "qty": 0},
        "RAPT-CON-RSW": {"name": "Right Switch Connector (34824-0125)", "qty": 0},
        "RAPT-CON-CSM": {"name": "Clock Spring Male Connector (30968-1167)", "qty": 0},
        "RAPT-CON-SHM": {"name": "Shifter Male Connector (30968-1127)", "qty": 0},
        "RAPT-CON-CSF": {"name": "Clock Spring Female Connector (30700-1167)", "qty": 0},
        "RAPT-CON-SHF": {"name": "Shifter Female Connector (30700-1120)", "qty": 0},
        "RAPT-CON-SCCM": {"name": "SCCM Female Connector (7287-2043-30)", "qty": 0},
        "RAPT-CON-PSB": {"name": "Paddle Shifter Black Connector (2138557-2)", "qty": 0},
        "RAPT-CON-PSG": {"name": "Paddle Shifter Grey Connector (2138557-1)", "qty": 0},
        "RAPT-CON-HORN": {"name": "Horn Connector (12059252)", "qty": 0},
        # Pin/terminal contacts (ordered from Mouser — qty per kit noted in name)
        "RAPT-PIN-LSW": {"name": "Left Switch Pins x12/kit (560023-0421)", "qty": 0},
        "RAPT-PIN-CSM": {"name": "Clock Spring Male Pins x10/kit (TE 2-1419158-5)", "qty": 0},
        "RAPT-PIN-CSF": {"name": "Clock Spring Female Pins x24/kit (TE 1393366-1)", "qty": 0},
        "RAPT-PIN-SCCM": {"name": "SCCM Female Pins x3/kit (TE 2035334-2)", "qty": 0},
        "RAPT-PIN-PS": {"name": "Paddle Shifter Pins x4/kit (2098762-1)", "qty": 0},
        "RAPT-PIN-HORN": {"name": "Horn Pins x2/kit (12059894-L)", "qty": 0},
        # Circuit boards (from Jason @ Cybernetworks)
        "RAPT-PCB-L": {"name": "Raptor Steering Wheel PCB — Left", "qty": 0},
        "RAPT-PCB-R": {"name": "Raptor Steering Wheel PCB — Right", "qty": 0},
    }
}

KITS = {
    "hot_pipes_sho": {
        "name": "SHO/Flex/MKT Hot Pipes", "shopify_id": "7786267443355", "retail_price": 625,
        "components": {"HP-NMD": 1, "HP-SHRT": 1, "HP-LNG": 1, "SH-0-51": 1, "SH-0-63": 1, "SH-0-76": 1,
                       "SR-0-38-51": 1, "SS-0-51": 1, "XH-CP024": 1, "CLAMP-150": 2, "CLAMP-200": 6, "CLAMP-250": 2, "CLAMP-300": 2}
    },
    "hot_pipes_explorer": {
        "name": "Explorer Sport Hot Pipes", "shopify_id": "7786267082907", "retail_price": 625,
        "components": {"HP-NMD": 1, "HP-SHRT": 1, "HP-LNG": 1, "SH-0-51": 1, "SH-0-76": 1, "SR-0-38-51": 1,
                       "SS-0-51": 1, "EXP-45-63": 1, "CLAMP-150": 1, "CLAMP-200": 5, "CLAMP-250": 2, "CLAMP-300": 2}
    },
    "nmd": {
        "name": "Noisemaker Delete Pipe", "shopify_id": "7786267050139", "retail_price": 235,
        "components": {"HP-NMD": 1, "SH-0-63": 1, "SH-0-76": 1, "CLAMP-250": 2, "CLAMP-300": 2}
    },
    "nmd_upgrade": {
        "name": "NMD to Hot Pipe Upgrade Kit", "shopify_id": "7786267312283", "retail_price": 500,
        "components": {"HP-SHRT": 1, "HP-LNG": 1, "SH-0-51": 1, "SR-0-38-51": 1, "SS-0-51": 1, "XH-CP024": 1,
                       "CLAMP-150": 2, "CLAMP-200": 6}
    },
    "explorer_nmd": {
        "name": "Explorer Sport NMD", "shopify_id": "8261715722395", "retail_price": 235,
        "components": {"HP-NMD": 1, "SH-0-76": 1, "EXP-45-63": 1, "CLAMP-250": 2, "CLAMP-300": 2}
    },
    "intake_stock_hose": {
        "name": "SHO/Flex/Explorer Intake (Stock Hose)", "shopify_id": "7786267213979", "shopify_variant": "stock", "retail_price": 660,
        "components": {"IN-HEAT": 1, "IN-S": 1, "IN-90": 1, "IN-STK": 1, "SH-0-63": 1, "SRE-90-45-63": 1,
                       "CLAMP-175": 1, "CLAMP-200": 1, "CLAMP-250": 5, "FILTER": 1}
    },
    "intake_custom_hose": {
        "name": "SHO/Flex/Explorer Intake (Custom Hose)", "shopify_id": "7786267213979", "shopify_variant": "custom", "retail_price": 700,
        "components": {"IN-HEAT": 1, "IN-S": 1, "IN-90": 1, "IN-CUST": 1, "SH-0-63": 1, "SRE-90-45-63": 1,
                       "XH-CP023": 1, "CLAMP-175": 1, "CLAMP-200": 1, "CLAMP-250": 6, "FILTER": 1}
    },
    "fusion_intake": {
        "name": "Fusion Sport 2.7L Intake Pipes", "shopify_id": "7786267181211", "retail_price": 385,
        "components": {"FU-I-PRT": 1, "FU-I-RCIRC": 1, "FU-I-PLN": 1, "SR-0-63-76": 2, "SRE-90-51-63": 2,
                       "SS-0-63": 1, "CLAMP-200": 2, "CLAMP-250": 6, "CLAMP-300": 2}
    },
    "fusion_charge": {
        "name": "Fusion Sport 2.7L Charge Pipes", "shopify_id": "7805538828443", "retail_price": 600,
        "components": {"FU-C-LNG": 1, "FU-C-MID90": 1, "FU-C-SML": 1, "FU-C-MCRO": 1, "FU-C-TBDY": 1,
                       "SR-0-48-51": 2, "SRE-90-63-70": 1, "SS-0-51": 4, "CLAMP-200": 12, "CLAMP-250": 3, "CLAMP-275": 1}
    },
    "f150_intake": {
        "name": "F150 3.5L Intake Tubes", "shopify_id": None, "retail_price": 860,
        "components": {"TR-I-BG": 1, "TR-I-LNG": 1, "SR-0-45-51": 1, "SR-0-45-63": 1, "CLAMP-175": 2, "CLAMP-200": 1, "CLAMP-250": 1}
    },
    "raptor_sw_harness": {
        "name": "Raptor Steering Wheel Harness", "shopify_id": None, "retail_price": 0,
        "components": {
            "RAPT-CON-LSW": 1, "RAPT-CON-RSW": 1, "RAPT-CON-CSF": 1,
            "RAPT-CON-PSB": 1, "RAPT-CON-PSG": 1, "RAPT-CON-HORN": 1,
            "RAPT-PIN-LSW": 12, "RAPT-PIN-CSF": 24,
            "RAPT-PIN-PS": 4, "RAPT-PIN-HORN": 2,
            "RAPT-PCB-L": 1, "RAPT-PCB-R": 1,
        }
    },
    "raptor_console_harness": {
        "name": "Raptor Console Shifter Harness", "shopify_id": None, "retail_price": 0,
        "components": {
            "RAPT-CON-CSM": 1, "RAPT-CON-SHM": 1,
            "RAPT-CON-SHF": 1, "RAPT-CON-SCCM": 1,
            "RAPT-PIN-CSM": 10, "RAPT-PIN-CSF": 10,
            "RAPT-PIN-SCCM": 3,
        }
    }
}


def seed_database():
    """Populate database with initial inventory data."""
    if Component.query.first():
        return False  # already seeded

    comp_map = {}
    for category, parts in COMPONENTS.items():
        for pn, info in parts.items():
            c = Component(
                part_number=pn,
                name=info['name'],
                category=category,
                old_pn=info.get('old_pn', ''),
                qty=info['qty'],
                reorder_threshold=10
            )
            db.session.add(c)
            comp_map[pn] = c

    db.session.flush()

    for slug, kit_info in KITS.items():
        kit = Kit(
            slug=slug,
            name=kit_info['name'],
            shopify_id=kit_info.get('shopify_id'),
            shopify_variant=kit_info.get('shopify_variant'),
            retail_price=kit_info.get('retail_price', 0)
        )
        db.session.add(kit)
        db.session.flush()

        for pn, qty in kit_info['components'].items():
            if pn in comp_map:
                kc = KitComponent(kit_id=kit.id, component_id=comp_map[pn].id, quantity=qty)
                db.session.add(kc)

    db.session.commit()
    return True
