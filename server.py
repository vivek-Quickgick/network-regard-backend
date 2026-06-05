# pyrefly: ignore [missing-import]
from fastapi import FastAPI, APIRouter, HTTPException
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv
# pyrefly: ignore [missing-import]
from starlette.middleware.cors import CORSMiddleware
# pyrefly: ignore [missing-import]
from motor.motor_asyncio import AsyncIOMotorClient
import os
import re
import logging
import urllib.parse
from pathlib import Path
# pyrefly: ignore [missing-import]
from pydantic import BaseModel, Field, ConfigDict
from typing import List
import uuid
from datetime import datetime, timezone
# pyrefly: ignore [missing-import]
import httpx
import xmlrpc.client
# pyrefly: ignore [missing-import]
import google.generativeai as genai
# pyrefly: ignore [missing-import]
from fastapi.middleware.cors import CORSMiddleware


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Configure logging early so middleware can use it
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Create the main app without a prefix
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://regardnetwork.vercel.app",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request logging middleware
@app.middleware("http")
async def log_requests(request, call_next):
    body = await request.body()
    logger.info(f"--> {request.method} {request.url.path}")
    if body:
        logger.info(f"    Body: {body.decode()}")
    response = await call_next(request)
    logger.info(f"<-- {response.status_code} {request.method} {request.url.path}")
    return response

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")


# Define Models
class StatusCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")  # Ignore MongoDB's _id field
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class StatusCheckCreate(BaseModel):
    client_name: str

class LeadCreate(BaseModel):
    name: str
    company: str
    service: str
    contact: str

class ContactCreate(BaseModel):
    name: str
    email: str
    phone: str = ""
    company: str = ""
    interest: str = ""
    message: str = ""

class TicketCreate(BaseModel):
    name: str
    company: str
    issue: str

class FAQRequest(BaseModel):
    message: str


# ── Validation helpers ─────────────────────────────────────────────────────────

def validate_email(email: str) -> str | None:
    """Returns error message if invalid, None if valid."""
    pattern = r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
    if not email or not re.match(pattern, email.strip()):
        return "Please provide a valid email address"
    return None


def validate_phone(phone: str) -> str | None:
    """Returns error message if invalid, None if valid (phone is optional)."""
    if not phone:
        return None
    digits = re.sub(r'\D', '', phone)
    if len(digits) < 7 or len(digits) > 15:
        return "Please provide a valid phone number (7-15 digits)"
    return None


def validate_contact(contact: str) -> str | None:
    """Validates a contact field that could be email or phone."""
    contact = contact.strip()
    if not contact:
        return "Please provide an email or phone number"
    if '@' in contact:
        return validate_email(contact)
    return validate_phone(contact)


# Add your routes to the router instead of directly to app
@api_router.get("/")
async def root():
    return {"message": "Hello World"}

# @api_router.post("/status", response_model=StatusCheck)
# async def create_status_check(input: StatusCheckCreate):
#     status_dict = input.model_dump()
#     status_obj = StatusCheck(**status_dict)
    
#     # Convert to dict and serialize datetime to ISO string for MongoDB
#     doc = status_obj.model_dump()
#     doc['timestamp'] = doc['timestamp'].isoformat()
    
#     _ = await db.status_checks.insert_one(doc)
#     return status_obj

# @api_router.get("/status", response_model=List[StatusCheck])
# async def get_status_checks():
#     # Exclude MongoDB's _id field from the query results
#     status_checks = await db.status_checks.find({}, {"_id": 0}).to_list(1000)
    
#     # Convert ISO string timestamps back to datetime objects
#     for check in status_checks:
#         if isinstance(check['timestamp'], str):
#             check['timestamp'] = datetime.fromisoformat(check['timestamp'])
    
#     return status_checks

# ── Sanity Blog Proxy ─────────────────────────────────────────────────────────

SANITY_PROJECT_ID = os.environ.get('SANITY_PROJECT_ID', '')
SANITY_DATASET = os.environ.get('SANITY_DATASET', 'production')
SANITY_TOKEN = os.environ.get('SANITY_TOKEN', '')
SANITY_API_VERSION = "2025-01-01"

BLOG_LIST_QUERY = """
*[_type == "post"] | order(publishedAt desc) {
  _id,
  title,
  "slug": slug.current,
  publishedAt,
  excerpt,
  mainImage,
  "estimatedReadingTime": round(length(pt::text(body)) / 5 / 180),
  "categories": categories[]->{
    _id,
    title,
    "slug": slug.current
  },
  author->{
    _id,
    name,
    "slug": slug.current
  }
}
"""

BLOG_DETAIL_QUERY = """
*[_type == "post" && slug.current == $slug][0]{
  _id,
  title,
  "slug": slug.current,
  publishedAt,
  excerpt,
  mainImage,
  body,
  "estimatedReadingTime": round(length(pt::text(body)) / 5 / 180),
  "categories": categories[]->{
    _id,
    title,
    "slug": slug.current
  },
  author->{
    _id,
    name,
    "slug": slug.current,
    image,
    bio
  }
}
"""

async def _sanity_fetch(query: str, params: dict = None):
    encoded_query = urllib.parse.quote(query.strip())
    url = f"https://{SANITY_PROJECT_ID}.api.sanity.io/v{SANITY_API_VERSION}/data/query/{SANITY_DATASET}?query={encoded_query}"
    if params:
        for k, v in params.items():
            url += f"&${k}={urllib.parse.quote(str(v))}"
    headers = {}
    if SANITY_TOKEN:
        headers["Authorization"] = f"Bearer {SANITY_TOKEN}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Sanity error: {resp.text[:200]}")
        return resp.json().get("result")


@api_router.get("/blog")
async def get_blog_posts():
    result = await _sanity_fetch(BLOG_LIST_QUERY)
    return result or []


@api_router.get("/blog/{slug}")
async def get_blog_post(slug: str):
    result = await _sanity_fetch(BLOG_DETAIL_QUERY, {"slug": slug})
    if not result:
        raise HTTPException(status_code=404, detail="Post not found")
    return result


FAQ_SYSTEM_PROMPT = "You are a helpful assistant for Regard Network Solution, an IT infrastructure company based in Delhi, India. You answer questions about their services, products, and company in a natural, conversational tone. Keep answers concise (2-4 sentences max). Do not use bullet points. If the question is completely unrelated to Regard Network or IT infrastructure, respond with exactly: FALLBACK"

RNS_CONTEXT = """
--- COMPANY IDENTITY ---
Full Legal Name   : Regard Network Solution Limited (formerly Regard Network Solution Pvt. Ltd.)
Brand Name        : Regard Network Solution
CIN               : U72200DL2011PLC221496
Founded           : 1998 (operational); incorporated 27 June 2011
Motto             : "Nation First, Make in India – Fostering Innovation, Employment, and Manufacturing Prowess."
Website           : www.regardnetwork.com
Email             : contact@regardnetwork.com
Phone             : +91-120-4974822 | +91-9140206504
Stock Status      : Listed (public limited company)

--- ADDRESSES ---
Registered Office       : G-37, Basement, Lajpat Nagar Part-1, Near Shubham Chemist,
                          New Delhi – 110024
Corporate / Head Office : Office No. 1404, Tower S3, Cloud9, Vaishali Sector-1,
                          Ghaziabad, Uttar Pradesh – 201014
Manufacturing Unit      : G-294, G Block, Sector-63, Noida, Uttar Pradesh – 201301
International Presence  : OMAN (active international operations)

--- ABOUT & MISSION ---
RNSL is an innovation-driven, client-centric company providing end-to-end Data Centre
Infrastructure, Networking, Security & Surveillance, and proprietary product manufacturing
solutions across India and abroad. The company is committed to empowering businesses
through scalable, future-ready Non-IT solutions tailored to modern enterprise needs.
Its skilled service professionals are strategically located across the country to ensure
seamless project execution and consistent service delivery.

--- COMPANY PHILOSOPHY ---
Core values guiding RNSL operations:
  • Ethical Business
  • Sound Decision Making
  • Respect for Human Rights
  • Risk Management
  • Health & Safety
  • Good Environment
  • Customer Satisfaction

--- SUCCESS MANTRA (4-Stage Model) ---
  01. Customer-Centric Approach & Excellence in Service
  02. Commitment to Quality & Empowering Partnership
  03. Innovation at Work
  04. Success

--- COMPANY HISTORY & MILESTONES ---
Phase         Year    Milestone
Regard 1.0    2000    Started with CCTV; pioneered segment relevance and continuous innovation
              2005    Expanded into Security & Surveillance; bank and corporate clientele
Regard 2.0    2010    Passive Networking; established need for end-to-end service providers
              2015    Data Centre Add-On; customised solutions (Rodent repellent, Fire suppression)
Regard 3.0    2020    Complete Data Centre Build; IBMS and CMS integration
              2025    One-stop turnkey project house; full DC lifecycle ownership

--- KEY STATISTICS ---
Years of Operation    : 25+ (founded 1998)
Revenue (FY 2024)     : ₹54 Crore
Revenue (FY 2023)     : ₹47 Crore
Revenue (FY 2022)     : ₹39 Crore
Revenue (FY 2021)     : ₹25 Crore
Revenue (FY 2020)     : ₹22 Crore
5-Year CAGR           : 24.5% (strong double-digit growth)
Clients Served        : 100+ satisfied clients across India
Regional Branches     : 6
Support Locations     : 300+
In-House Products     : 350+ self-built products (ordered by companies in India & abroad)
Certifications        : CMMI Level 3, ISO 45001:2018, ISO 27001, ISO 9001:2015,
                        ISO 14001:2015, RoHS Compliant, FCC, CE, QVS Certified
Personnel Certifications : CDCP & PMP certified leadership

--- CORE LEADERSHIP ---
Name              Title                  Background
Pawan Dubey       Founder & CEO/MD       25 years industry experience; Data Centre Specialist;
                                         Technology Innovator; CDCP Certified
Pankaj Kumar      Executive Director     20+ years in Data Centre Build; ex-Sify, Nxtgen, Reliance;
                                         CDCP Certified; appointed Director Jul 2025
Arunish Pandey    COO                    20+ years in Pharma & other sectors; ex-GSK,
                                         Johnson & Johnson, SUN Pharma
Anupam Mishra     Legal Advisor          20+ years legal expertise; Advocate at Supreme Court of India

--- ORGANISATION STRUCTURE ---
Managing Director (Pawan Dubey)
  ├── COO
  │     ├── Procurement Team → Development & Manufacturing Team
  │     ├── Service Team → Customer Help Desk → Technical Team
  │     ├── HR → Admin Team → ERP Team
  │     └── Finance Department → Account Team
  └── Executive Director (Strategic Initiative)
        ├── Sales → Branch Head → Sales Manager → Govt. Affairs & Bid Team
        └── Project Team → On-Site Support Team

--- TECHNOLOGY SERVICES ---
1. Data Centre Management
   - Critical Facilities Consulting: capacity sizing, redundancy planning,
     uptime/availability selection, site/location selection, design topology,
     DC migration planning, cost projections, trusted advisory
   - Critical Facilities Design: site/facility evaluation for enterprise programs,
     reliability & cost modelling, detailed engineering documents, architectural
     layouts, BOM/BOQ enablement, peer review
   - Critical Facilities Assurance: operational performance optimisation, energy
     efficiency, reliability audits, staffing, maintenance programs, continuous
     improvement
   - Critical Facilities Implementation: program management, owner's
     representative, turnkey design/build, DC equipment migration to new facility
   - Additional DC Services: audit service for Power & Cooling; assess & design
     datacenter solution; DC Construction & Integration; Green Datacenter creation;
     Datacenter Certification Process enablement

2. System Integration & Rack Solutions
   - Server rack design, integration, and deployment

3. Active & Passive Networking
   - Structured Cabling (UTP, Fibre Optic)
   - MPOS Solution, Raceways, Fiber Runner
   - Software Defined Network (SDN)
   - Low Latency Network
   - SAN Networking
   - Notable Projects: Tech-Mahindra (Noida, 2350 ports), Axis Max Life Insurance
     (Gurugram, 2797 ports; PAN India, 3960 ports), Hewlett Packard Enterprise
     (MHA Delhi & Nagpur, 1382 ports each)

4. Central Monitoring & Management System (CMMS)
   - Connects cameras and third-party devices (Server Door, Fire Suppressor,
     Diesel Monitoring, etc.) across multiple locations at a central hub
   - 24×7 CMS Remote monitoring
   - Siren/Hooter remote control; Two-way audio with remote DVR
   - Branch opening/closing report with images
   - Web-based application for remote video/image data pulling

5. Infrastructure Solutions
   - HVAC: Precision Cooling Systems, Comfort AC, Chillers, InRow, RDHx
   - Power infrastructure
   - Civil: DC Floor Assessment
   - Cabling: Fiber, UTP, MPOS Solution, Raceways, Fiber Runner

6. Security & Surveillance
   - CCTV systems
   - Fire Alarm & Suppression (HSSD/VESDA)
   - Access Control systems
   - Rodent Repellent Systems
   - DCIM / BMS integration

--- DCIS (DATA CENTRE INTEGRATION SERVICES) BUILDING BLOCKS ---
TIS Data Centre Integration Services comprises:
  • Managed Services: Service Desk, Monitoring Solution (DC, Applications, DB)
  • Non-IT Solutions
  • Civil: DC Floor Assessment
  • Power
  • HVAC: Precision Cooling, Comfort AC, Chillers, InRow, RDHx
  • Security: Fire Alarm Panel, Fire Suppression, HSSD/VESDA, Access Control,
              Rodent Repellent, DCIM/BMS
  • Cabling: Fiber, UTP, MPOS Solution, Raceways, Fiber Runner
  • Network: SDN, Low Latency, SAN Networking

--- WHY RNSL FOR NON-IT DC BUILD ---
  • Single Window Service Provider & Aggregator
  • Partnered with Leading Technology OEMs
  • Faster Turnaround
  • Flexible Change Management
  • Transparency in Governance
  • Unbiased Evaluation of Technologies

--- PROPRIETARY PRODUCTS ---

1. RIM360 — Rack Intelligent Monitoring Solution
   Overview: State-of-the-art all-in-one monitoring solution for mission-critical
   rack environments; provides real-time oversight of environmental and system
   parameters to ensure optimal operational conditions.
   Key Features:
     - Temperature & Humidity Monitoring: High-precision sensors, configurable thresholds
     - Water Leak Detection (WLD): Cable-based and pad-based detection; alarm <1 sec
     - Front & Back Door Status Alarm: Magnetic door sensors; unauthorized access alerts
     - Rack Air-Conditioning Monitoring: RS485 Modbus integration with cooling units
     - UPS & IPDU Monitoring: SNMP-based power monitoring
     - Data Logging & Alerts: Comprehensive logging + MQTT integration
     - Protocol Support: RS485 Modbus RTU, SNMP v2/v3, MQTT
   Technical Specs:
     - Temp Range: -10°C to +60°C; Accuracy: ±0.5°C
     - Humidity: 0%–99% RH; Accuracy: ±3% RH
     - Water Leak Alarm Response: <1 second
     - Door Monitoring: Magnetic reed sensors
     - Data Comm: RS485 Modbus RTU (9600–115200 bps); SNMP Traps; MQTT (JSON)
     - Power: 9–24V DC; <5W consumption
     - Dimensions: 200mm × 150mm × 50mm; Weight: 1.2 kg
     - Alerts: Audible buzzer + LED indicators + SNMP traps + MQTT messages
   Applications: Data Centers, Telecom Racks, Industrial Control Systems
   Compliance: CE, FCC, RoHS

2. Server Room in a Box (Protector Series / IOT Rack)
   Overview: Fully enclosed, pre-built, plug-and-play IT Rack Enclosure designed
   for small server rooms to large enterprise data centres; saves time and space
   in premises, branches, and office areas.
   Core Concept: "Why spend Lakhs on setting up an entire server room
   when you can have the server in a box."
   Sizes: 12U to 45U (intelligent racks)
   Key Attributes: Plug & Play | Pre-Built | Space Saver | Intelligent Cabling |
                   Automated Functionality
   IOT RACK (PROFUSION) Features:
     - Micro modular data centre <10 sqft / 1 sqm footprint
     - Self-contained, precision-cooled, high-density
     - Integrated redundant cooling (2N, N+1, N configurations)
     - Hot-swappable cooling modules at top or bottom; 30kW cooling capacity
   Advanced Security & Control:
     - Monitoring, Mounting, Cooling, Security, Easy Troubleshooting
     - Temperature parameter adjustment
     - Automatic Door opening at high temperatures
     - Centralized Control
     - Rack Opening and Closing Log Data
   Applications: Branch offices, remote sites, edge computing, small enterprises

3. GRLS — Gas Release Fire Suppression Panel (Model: RNSGRLS2023V1.3)
   Overview: Self-contained Fire Detection and Suppression System for server
   rack protection from fire hazards; 3U rack-mountable unit.
   Clean Agent: FK-5-1-12 — eco-friendly, leaves no residue, avoids cleanup
   Key Features:
     - LCD Display with fire relay + 2 programmable outputs
     - Battery backup: 5 hours with low-battery visual + audible alert
     - Gas inhibit and instant release facility
     - Optical Smoke Detectors with cross-zoning
     - Supports 1.5m³ and 3m³ volume coverage
     - Includes cylinder, actuator, nozzle, releasing circuit
   Technical Specifications:
     - Operating Pressure: 240 psig (16.5 Bar) at 21°C
     - Operating Temp: 0°C to +50°C
     - Power Supply: 220–240V AC, 3A, 60/50 Hz
     - Size: 3U × 19" chassis
   Applications: Smart Cabinet Racks, Independent IT Racks
   Compliance: CE, FCC, RoHS

4. Temperature & Humidity Panel (THP)
   - Environmental monitoring panel for temperature and humidity control
   - CE, RoHS, FCC certified

5. Building Management System (BMS)
   - Integrated control panel for datacenter building management
   - Monitoring and control of facility-wide systems

6. Fire Suppression Panel (standalone)
   - Gas Release (clean agent) suppression panel for rack/room-level protection

7. Fiber Runner
   - Proprietary cable management accessory for structured cabling deployments

--- KEY PROJECTS EXECUTED (DC BUILD) ---
S.No  Customer           Zone         Location       Year  Area(sqft)  Racks  Tier      Scope
1     GAIL               North        Noida          2016  2500        34     TIER 3    Rack,PDU,PAC,IBMS,Civil
2     UKSDC              North        Dehradun       2018  2000        12     TIER 3    Rack,PDU,PAC,IBMS,Civil
3     SGT University     North        Noida          2021  364         6      Standard  Rack,PDU,PAC,IBMS,Civil
4     Concor             North        Delhi          2022  2000        28     Standard  Rack,PDU,PAC,IBMS,Civil
5     IAF                North        Gurgaon        2024  2200        12     Standard  Rack,PDU,PAC,IBMS,Civil
6     MHA                North        Delhi          2024  2200        24     Standard  Rack,PDU,PAC,IBMS,Civil
7     IIT                North        Sonipat        2025  2545        26     Standard  Rack,PDU,PAC,IBMS,Civil
8     ICG                North/South  Delhi/Manglore 2025  4845        26     TIER 3    Rack,PDU,PAC,IBMS,Electrical,Cooling,Civil
9     WB-SDC             East         Kolkata        2018  3450        52     TIER 3    Rack,PDU,PAC,IBMS,Civil
10    Concor             West         Nagpur         2020  2500        22     Standard  Rack,PDU,PAC,IBMS,Civil
11    MHA                West         Nagpur         2024  1800        24     Standard  Rack,PDU,PAC,IBMS,Civil
12    MTNL               South        Chennai        2022  2000        12     Standard  Rack,PDU,PAC,IBMS,Civil
13    DUK                South        Kerala         2024  2100        12     Standard  Rack,PDU,PAC,IBMS,Civil
14    HAL                South        Bangalore      2025  2375        11     Standard  Rack,PDU,PAC,IBMS,Civil

Other Notable Projects (by service type):
  DC Build: Gail India, Dherdun Smart City (Security & Surveillance),
            IIT-Sonipat (DC Build + Security + Networking),
            HAL Bangalore, Concor Nagpur, BSES, OTPC, Kerala SDC,
            Odissa SDC, Assam SDC, Punjab SDC-Mohali, MHA-DR Nagpur
  Networking: Axis Max Life (Security + Networking + System Integration),
              JNU University, SGT University
  Security & Surveillance: Bharti Axa, Emerson Process Management, ICICI Hyderabad
  Passive Networking Reference Sites: Punjab SDC-Mohali, IIT-Sonipat,
            WB-SDC, GAIL-Noida, New Delhi (Concor), HAL Bangalore, MTNL-Chennai,
            DUK Kerala, MHA-DR Nagpur

--- TRUSTED CLIENTS (SAMPLE) ---
Government & Defence   : CONCOR, Indian Coast Guard, Ministry of Home Affairs (MHA),
                         IAF (International Accreditation Forum), ITDA-CALC (Uttarakhand Govt.),
                         ONGC, Indian Oil, BSES, MTNL, OTPC, WB-SDC, Kerala SDC,
                         Odisha SDC, Assam SDC, Punjab SDC, IIT Jammu, IIT Sonipat
Healthcare             : Max Healthcare, Fortis, Sharda Hospital, Dr. Reddy's, Sun Pharma,
                         Pi Health Sciences, Zepto, Tata 1mg
Banking & Finance      : ICICI Bank, Axis Max Life Insurance, Bharti AXA, Grant Thornton
Technology & Industry  : C-DOT, Sify, ODC, Schneider Electric, Vertiv, HP, Emerson,
                         LDC (Louis Dreyfus), Motherson, Dainik Jagran
Education              : JNU, SGT University

--- PARTNERED OEM BRANDS ---
Power & Cooling   : Vertiv, Schneider Electric, Delta, Emerson, Bry-Air, Purafil
Networking        : Commscope, D-Link, 3C3
Fire & Safety     : Kidde Fire Systems, Johnson Controls (Tyco), Cryptzo
Access & Security : Hikvision, CP Plus, Dahua Technology, Honeywell, Rosslare,
                    Sparsh, Spectra, Smart-i (SIS)
Rack & Systems    : Maser, C-Systems (Innovative Embedology), Swastik Synergy

--- PARTNER COMPANIES ---
1. DTown Robotics Pvt. Ltd. (DTR)
   - DGCA Type-Certified drone and robotics solutions company
   - Specialises in unmanned solutions with camera payloads, LiDAR technology,
     and remote-controlled weapon systems
   - Sectors: Defence, Agriculture, and beyond

2. Things Horizon
   - Advanced industrial automation and smart infrastructure solutions
   - Track record across Technology, Finance, Manufacturing, Healthcare, Energy, Education
   - Offerings: Innovative Technology, End-to-End Expertise, Scalable & Future-Ready,
                Quality & Reliability

--- MANUFACTURING CAPABILITY ---
  • In-house R&D and manufacturing of 350+ products
  • Products: GRLS (Gas Release Fire Suppression), THP (Temperature Humidity Panel),
              IOT Rack, RIM360, Fiber Runner, BMS
  • Development cycle: Idea → Research → Develop → Test → Result
  • Key manufacturing clients: Vertiv, Delta, Schneider, Dr. Reddy's, AMLI,
                               Sharda University

--- CERTIFICATIONS & COMPLIANCE ---
ISO 45001:2018        (Environmental / Health & Safety)
ISO 27001             (Information Security Management)
ISO 9001:2015         (Quality Management / Certified Company)
ISO 14001:2015        (Environmental Management)
RoHS Compliant        (Restriction of Hazardous Substances)
CMMI Level 3          (Process & Standardised Practice)
FCC                   (Federal Communications Commission)
CE                    (European Conformity)
QVS Certified
CDCP & PMP           (Leadership-level professional certifications)
Tagline               : "We deliver peak security and reliability — a promise you can trust."

--- CONTACT INFORMATION ---
Website        : www.regardnetwork.com
Email          : contact@regardnetwork.com
Phone          : +91-120-4974822 | +91-120-5114598 | +91-9140206504
Regd. Office   : G-37, Lajpat Nagar Part-1, New Delhi – 110024
Corp. Office   : Cloud-9, Vaishali Sector-1, Ghaziabad, U.P.
Mfg. Unit      : G-294, G Block, Sector-63, Noida, U.P. – 201301

"""

@api_router.post("/contact")
async def submit_contact_form(input: ContactCreate):
    # Validate email
    email_error = validate_email(input.email)
    if email_error:
        return {"success": False, "error": email_error}

    # Validate phone (if provided)
    phone_error = validate_phone(input.phone)
    if phone_error:
        return {"success": False, "error": phone_error}

    try:
        logger.info(f"Contact form submission: {input.name} <{input.email}> | Interest: {input.interest}")
        odoo_url = os.environ.get("ODOO_URL", "https://network-regard.odoo.com")
        odoo_db = os.environ.get("ODOO_DB", "network-regard")
        odoo_username = os.environ.get("ODOO_USERNAME", "devansh.regard@gmail.com")
        odoo_password = os.environ.get("ODOO_PASSWORD", "")

        odoo_host = odoo_url.replace("https://", "").replace("http://", "")

        description_parts = []
        if input.interest:
            description_parts.append(f"Interest: {input.interest}")
        if input.phone:
            description_parts.append(f"Phone: {input.phone}")
        if input.company:
            description_parts.append(f"Company: {input.company}")
        if input.message:
            description_parts.append(f"\nMessage:\n{input.message}")
        description = "\n".join(description_parts)

        if not odoo_password or odoo_password == "your_odoo_api_key_here":
            logger.info("Mocking Odoo CRM Lead creation (contact form) due to missing/default Odoo password.")
            lead_id = 77777
        else:
            common = xmlrpc.client.ServerProxy(f"https://{odoo_host}/xmlrpc/2/common")
            uid = common.authenticate(odoo_db, odoo_username, odoo_password, {})
            if not uid:
                raise Exception("Authentication failed: invalid Odoo credentials or API key")

            models = xmlrpc.client.ServerProxy(f"https://{odoo_host}/xmlrpc/2/object")
            lead_id = models.execute_kw(
                odoo_db, uid, odoo_password,
                'crm.lead', 'create', [{
                    'name': f'Website Enquiry - {input.interest or "General"} | {input.company or input.name}',
                    'contact_name': input.name,
                    'partner_name': input.company or input.name,
                    'email_from': input.email,
                    'phone': input.phone,
                    'description': description,
                }]
            )

        return {"success": True, "leadId": lead_id}
    except Exception as e:
        logger.error(f"Odoo contact form submission error: {str(e)}")
        return {"success": False, "error": f"Failed to submit enquiry: {str(e)}"}


@api_router.post("/leads")
async def create_lead(input: LeadCreate):
    # Validate contact (could be email or phone)
    contact_error = validate_contact(input.contact)
    if contact_error:
        return {"success": False, "error": contact_error}

    try:
        print(input)
        odoo_url = os.environ.get("ODOO_URL", "https://network-regard.odoo.com")
        odoo_db = os.environ.get("ODOO_DB", "network-regard")
        odoo_username = os.environ.get("ODOO_USERNAME", "devansh.regard@gmail.com")
        odoo_password = os.environ.get("ODOO_PASSWORD", "")
        
        
        odoo_host = odoo_url.replace("https://", "").replace("http://", "")
        
        if not odoo_password or odoo_password == "your_odoo_api_key_here":
            logger.info("Mocking Odoo CRM Lead creation due to missing/default Odoo password.")
            lead_id = 99999
        else:
            common = xmlrpc.client.ServerProxy(f"https://{odoo_host}/xmlrpc/2/common")
            uid = common.authenticate(odoo_db, odoo_username, odoo_password, {})
            if not uid:
                raise Exception("Authentication failed: invalid Odoo credentials or API key")
            
            models = xmlrpc.client.ServerProxy(f"https://{odoo_host}/xmlrpc/2/object")
            lead_id = models.execute_kw(
                odoo_db, uid, odoo_password,
                'crm.lead', 'create', [{
                    'name': f'Website Lead - {input.company}',
                    'contact_name': input.name,
                    'partner_name': input.company,
                    'description': f'Service: {input.service} | Contact: {input.contact}',
                }]
            )
        
        return {"success": True, "leadId": lead_id}
    except Exception as e:
        logger.error(f"Odoo CRM Lead creation error: {str(e)}")
        return {"success": False, "error": f"Failed to submit lead: {str(e)}"}

@api_router.post("/tickets")
async def create_ticket(input: TicketCreate):
    print(input)
    try:
        odoo_url = os.environ.get("ODOO_URL", "https://network-regard.odoo.com")
        odoo_db = os.environ.get("ODOO_DB", "network-regard")
        odoo_username = os.environ.get("ODOO_USERNAME", "devansh.regard@gmail.com")
        odoo_password = os.environ.get("ODOO_PASSWORD", "")
        
        odoo_host = odoo_url.replace("https://", "").replace("http://", "")
        
        if not odoo_password or odoo_password == "your_odoo_api_key_here":
            logger.info("Mocking Odoo Helpdesk Ticket creation due to missing/default Odoo password.")
            ticket_id = 88888
        else:
            common = xmlrpc.client.ServerProxy(f"https://{odoo_host}/xmlrpc/2/common")
            uid = common.authenticate(odoo_db, odoo_username, odoo_password, {})
            if not uid:
                raise Exception("Authentication failed: invalid Odoo credentials or API key")
            
            models = xmlrpc.client.ServerProxy(f"https://{odoo_host}/xmlrpc/2/object")
            ticket_id = models.execute_kw(
                odoo_db, uid, odoo_password,
                'helpdesk.ticket', 'create', [{
                    'name': f'Support - {input.company}',
                    'partner_name': input.name,
                    'description': input.issue,
                }]
            )
        
        return {"success": True, "ticketId": ticket_id}
    except Exception as e:
        logger.error(f"Odoo Helpdesk Ticket creation error: {str(e)}")
        return {"success": False, "error": f"Failed to create support ticket: {str(e)}"}

@api_router.post("/faq")
async def get_faq_answer(input: FAQRequest):
    try:
        gemini_api_key = os.environ.get("GEMINI_API_KEY")
        if not gemini_api_key or gemini_api_key == "your_gemini_api_key_here":
            logger.info("Mocking Gemini response due to missing/default Gemini API key.")
            msg = input.message.lower()
            if "services" in msg or "offer" in msg or "what do you do" in msg:
                answer = "Regard Network Solution offers Data Center Solutions, Facility Management Services, Passive Networking, Fire Suppression Systems, and Smart City Solutions."
                fallback = False
            elif "government" in msg or "gem" in msg:
                answer = "Yes, we work with government agencies and are listed on the Government e-Marketplace (GeM)."
                fallback = False
            elif "typical project" in msg or "how long" in msg:
                answer = "A typical project timeline varies depending on the scale and complexity, but structured cabling or cooling setup usually takes 2 to 4 weeks."
                fallback = False
            elif "delhi" in msg or "offices" in msg or "located" in msg:
                answer = "Our corporate office is located in Lajpat Nagar, New Delhi. We also have a presence and locations across other states in India."
                fallback = False
            elif "facility management" in msg or "what is" in msg:
                answer = "Facility Management Services include comprehensive monitoring and maintenance of your IT infrastructure and physical server environments."
                fallback = False
            else:
                answer = ""
                fallback = True
            return {"answer": answer, "fallback": fallback}
        
        genai.configure(api_key=gemini_api_key)
        
        model = genai.GenerativeModel(
            model_name='gemini-2.5-flash-lite',
            system_instruction=FAQ_SYSTEM_PROMPT
        )
        
        import asyncio
        
        def call_gemini():
            response = model.generate_content(
                RNS_CONTEXT + '\n\nUser question: ' + input.message
            )
            return response.text.strip()
            
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, call_gemini)
        
        fallback = text == 'FALLBACK'
        return {"answer": "" if fallback else text, "fallback": fallback}
        
    except Exception as e:
        logger.error(f"Gemini API error: {str(e)}")
        return {"answer": "", "fallback": True}

# Include the router in the main app
app.include_router(api_router)

# Configure logging
# (logging is now initialised near the top of this file)

# @app.on_event("shutdown")
# async def shutdown_db_client():
#     client.close()