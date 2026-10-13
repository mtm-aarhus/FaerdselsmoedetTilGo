"""robot_framework/faerdselsmoede_pdf.py - læser et møde fra Færdselsmødets database og
laver dagsorden/referat som PDF.

PDF-koden er en kopi af _build_agenda_pdf / _build_referat_pdf / _letterhead_story i
web-appens app/blueprints/meetings/routes.py. Ændres layoutet i web-appen, skal det
også ændres her - ellers ser dokumentet i GO anderledes ud end det deltagerne fik.

Databasen angives som enten
    - en sti til en SQLite-fil (midlertidigt: en kopi af web-appens faerdselsmoede_dev.db), eller
    - en SQLAlchemy-URL (senere: mssql+pyodbc://... til produktionsdatabasen).
Databasen åbnes altid skrivebeskyttet - robotten ændrer intet i den.
"""
import io
import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, text

LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aarhus_logo.png")

PROPOSAL_STATUS_LABELS = {
    'kladde': 'Kladde',
    'indsendt': 'Indsendt',
    'i_hoering': 'I høring',
    'hoering_afsluttet': 'Høring afsluttet',
    'paa_dagsorden': 'På dagsorden',
    'godkendt': 'Godkendt',
    'afvist': 'Afvist',
    'udsat': 'Udsat',
}

_DK_MONTHS = ['januar', 'februar', 'marts', 'april', 'maj', 'juni', 'juli',
              'august', 'september', 'oktober', 'november', 'december']


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def opret_engine(database: str):
    """Laver en skrivebeskyttet SQLAlchemy-engine ud fra en filsti eller en URL."""
    if "://" in database:
        return create_engine(database, future=True)

    sti = Path(database)
    if not sti.is_absolute():
        # Relativ sti = i forhold til robottens mappe (den med main.py)
        sti = Path(__file__).resolve().parent.parent / sti
    if not sti.exists():
        raise FileNotFoundError(f"Databasefilen findes ikke: {sti}")
    # mode=ro: SQLite åbnes skrivebeskyttet, så kopien aldrig ændres ved en fejl
    return create_engine(f"sqlite:///file:{sti.as_posix()}?mode=ro&uri=true", future=True)


def _dt(vaerdi):
    """SQLite giver datoer som tekst, SQL Server som datetime - begge bliver til datetime."""
    if vaerdi is None or isinstance(vaerdi, datetime):
        return vaerdi
    return datetime.fromisoformat(str(vaerdi))


def hent_moede(engine, meeting_id: str):
    """Henter mødet med deltagere, referat og indstillinger.
    Returnerer None, hvis mødet ikke findes."""
    with engine.connect() as conn:
        m = conn.execute(text(
            "SELECT id, meeting_number, meeting_date, duration_minutes, location "
            "FROM meeting WHERE id = :id"), {"id": meeting_id}).mappings().first()
        if not m:
            return None

        deltagere = conn.execute(text(
            "SELECT p.name FROM participant p "
            "JOIN meeting_participant mp ON mp.participant_id = p.id "
            "WHERE mp.meeting_id = :id"), {"id": meeting_id}).mappings().all()

        referat = conn.execute(text(
            "SELECT content FROM meeting_minutes WHERE meeting_id = :id"),
            {"id": meeting_id}).mappings().first()

        rows = conn.execute(text(
            "SELECT id, address, addresses, applicant, project_name, description, proposal_text, "
            "meeting_attendee, status, agenda_order, agreement, decision, outcome_status, submitted_at "
            "FROM proposal WHERE meeting_id = :id"), {"id": meeting_id}).mappings().all()

        indstillinger = []
        for r in rows:
            bilag = conn.execute(text(
                "SELECT original_filename FROM attachment WHERE proposal_id = :pid ORDER BY id"),
                {"pid": r["id"]}).mappings().all()
            indstillinger.append(_indstilling(r, [b["original_filename"] for b in bilag]))

    meeting_date = _dt(m["meeting_date"])
    slut = meeting_date + timedelta(minutes=m["duration_minutes"] or 120)
    moede = SimpleNamespace(
        id=m["id"],
        meeting_number=m["meeting_number"],
        meeting_date=meeting_date,
        location=m["location"],
        label=f"FM {m['meeting_number']}",
        time_range=f"{meeting_date.strftime('%H:%M')}-{slut.strftime('%H:%M')}",
        participants=[SimpleNamespace(name=d["name"]) for d in deltagere],
        minutes=SimpleNamespace(content=referat["content"]) if referat else None,
    )
    # Samme rækkefølge som referatpakken i web-appen (_send_meeting_package)
    indstillinger.sort(key=lambda p: (p.agenda_order or 999, p.submitted_at or datetime.min))
    return moede, indstillinger


def _indstilling(r, bilag_navne):
    adresser = [a.strip() for a in (r["addresses"] or r["address"] or "").splitlines() if a.strip()]
    return SimpleNamespace(
        project_name=r["project_name"],
        address=r["address"],
        address_display=", ".join(adresser) or (r["address"] or ""),
        applicant=r["applicant"],
        meeting_attendee=r["meeting_attendee"],
        status=r["status"],
        status_label=PROPOSAL_STATUS_LABELS.get(r["status"], r["status"]),
        description=r["description"],
        proposal_text=r["proposal_text"],
        agreement=r["agreement"],
        decision=r["decision"],
        outcome_status=r["outcome_status"],
        agenda_order=r["agenda_order"],
        submitted_at=_dt(r["submitted_at"]),
        attachments=[SimpleNamespace(original_filename=n) for n in bilag_navne],
    )


# ---------------------------------------------------------------------------
# PDF (kopi af web-appens kode)
# ---------------------------------------------------------------------------

def _letterhead_story(styles):
    """Aarhus Kommune brevhoved: byvåben (stempel) + Teknik og Miljø-kontaktblok."""
    from reportlab.platypus import Image, Table, TableStyle, Paragraph, Spacer, HRFlowable
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT

    d = datetime.now()
    date_str = f'{d.day}. {_DK_MONTHS[d.month - 1]} {d.year}'
    cred_style = ParagraphStyle('cred', parent=styles['Normal'], fontSize=8, leading=11,
                                alignment=TA_RIGHT, textColor=colors.HexColor('#333333'))
    cred = Paragraph(
        f'{date_str}<br/><br/>'
        '<b>TEKNIK OG MILJØ</b><br/>Byrum, Aarhus Kommune<br/><br/>'
        '<b>Anlæg</b><br/>Karen Blixens Boulevard 7<br/>8220 Brabrand<br/><br/>'
        'Telefon: 89 40 44 00<br/><br/>'
        'E-mail:<br/>anlaeg@mtm.aarhus.dk<br/>www.aarhus.dk',
        cred_style)

    if os.path.exists(LOGO_PATH):
        logo = Image(LOGO_PATH, width=2.2 * cm, height=2.2 * cm * 289 / 205)
        header = Table([[logo, cred]], colWidths=[3 * cm, 14 * cm])
    else:
        header = Table([['', cred]], colWidths=[3 * cm, 14 * cm])
    header.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
    ]))
    return [header, Spacer(1, 0.2 * cm),
            HRFlowable(width='100%', color=colors.HexColor('#003764'), thickness=1.5),
            Spacer(1, 0.35 * cm)]


def _numbered_canvas_class():
    from reportlab.pdfgen import canvas as _rl_canvas

    class _NumberedCanvas(_rl_canvas.Canvas):
        """Tilføjer 'Side X af Y' i bunden af hver side."""
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_states = []

        def showPage(self):
            self._saved_states.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.units import cm
            total = len(self._saved_states)
            for state in self._saved_states:
                self.__dict__.update(state)
                self.setFont('Helvetica', 8)
                self.setFillColorRGB(0.4, 0.4, 0.4)
                self.drawCentredString(A4[0] / 2.0, 1.1 * cm, f'Side {self._pageNumber} af {total}')
                super().showPage()
            super().save()

    return _NumberedCanvas


def build_agenda_pdf(meeting, proposals) -> bytes:
    """Dagsordenen som PDF (reportlab)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle, HRFlowable)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    BLUE = colors.HexColor('#003764')
    title_style = ParagraphStyle('t', parent=styles['Heading1'], textColor=BLUE, fontSize=18, spaceAfter=6)
    h2 = ParagraphStyle('h2', parent=styles['Heading2'], textColor=BLUE, fontSize=13, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle('b', parent=styles['Normal'], fontSize=10, leading=14)
    label = ParagraphStyle('l', parent=styles['Normal'], fontSize=9, textColor=colors.grey)

    story = _letterhead_story(styles)
    story.append(Paragraph(f'Dagsorden – {meeting.label}', title_style))
    story.append(Paragraph(
        f'{meeting.meeting_date.strftime("%A d. %d. %B %Y")}, {meeting.time_range}  ·  {meeting.location or ""}',
        body))
    if meeting.participants:
        story.append(Paragraph('<b>Deltagere:</b> ' + ', '.join(p.name for p in meeting.participants), body))
    story.append(Spacer(1, 0.3*cm))
    story.append(HRFlowable(width='100%', color=BLUE, thickness=1.5))
    story.append(Paragraph(f'Dagsordenspunkter ({len(proposals)})', h2))

    for idx, p in enumerate(proposals, start=1):
        story.append(Spacer(1, 0.25*cm))
        story.append(Paragraph(f'{idx}. {p.project_name or p.address or "(uden titel)"}', h2))
        rows = [
            ['Adresse', p.address_display or '—'],
            ['Ansøger / Projektleder', p.applicant or p.meeting_attendee or '—'],
            ['Status', p.status_label],
        ]
        tbl = Table(rows, colWidths=[4*cm, 13*cm])
        tbl.setStyle(TableStyle([
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('TEXTCOLOR', (0, 0), (0, -1), colors.grey),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ]))
        story.append(tbl)
        if p.description or p.proposal_text:
            story.append(Spacer(1, 0.15*cm))
            story.append(Paragraph('<b>Beskrivelse</b>', label))
            story.append(Paragraph((p.description or p.proposal_text or '').replace('\n', '<br/>'), body))
        story.append(HRFlowable(width='100%', color=colors.lightgrey, thickness=0.5))

    doc.build(story, canvasmaker=_numbered_canvas_class())
    return buf.getvalue()


def build_referat_pdf(meeting, proposals) -> bytes:
    """Referatet som PDF (reportlab)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle, HRFlowable)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)

    styles = getSampleStyleSheet()
    BLUE = colors.HexColor('#003764')

    title_style = ParagraphStyle('title', parent=styles['Heading1'],
                                 textColor=BLUE, fontSize=18, spaceAfter=6)
    h2_style    = ParagraphStyle('h2', parent=styles['Heading2'],
                                 textColor=BLUE, fontSize=13, spaceBefore=12, spaceAfter=4)
    body_style  = ParagraphStyle('body', parent=styles['Normal'], fontSize=10, leading=14)
    label_style = ParagraphStyle('label', parent=styles['Normal'],
                                 fontSize=9, textColor=colors.grey)

    story = _letterhead_story(styles)

    # Titel
    story.append(Paragraph(f'Referat – {meeting.label}', title_style))
    story.append(Paragraph(
        f'{meeting.meeting_date.strftime("%A d. %d. %B %Y")}, {meeting.time_range}  ·  {meeting.location or ""}',
        body_style))
    story.append(Spacer(1, 0.3*cm))

    # Deltagere
    if meeting.participants:
        names = ', '.join(p.name for p in meeting.participants)
        story.append(Paragraph(f'<b>Deltagere:</b> {names}', body_style))
    story.append(Spacer(1, 0.4*cm))

    # Bemærkninger
    if meeting.minutes and meeting.minutes.content:
        story.append(Paragraph('Bemærkning til sidste referat', h2_style))
        story.append(Paragraph(meeting.minutes.content.replace('\n', '<br/>'), body_style))
        story.append(Spacer(1, 0.3*cm))

    story.append(HRFlowable(width='100%', color=BLUE, thickness=1.5))
    story.append(Spacer(1, 0.3*cm))

    # Dagsordenspunkter
    story.append(Paragraph(f'Dagsordenspunkter ({len(proposals)})', h2_style))

    for idx, p in enumerate(proposals, start=1):
        story.append(Spacer(1, 0.3*cm))
        story.append(Paragraph(f'{idx}. {p.project_name or p.address or "(uden titel)"}', h2_style))

        rows = [
            ['Adresse', p.address or '—'],
            ['Ansøger / Projektleder', p.applicant or p.meeting_attendee or '—'],
            ['Status', p.status_label],
        ]
        tbl = Table(rows, colWidths=[4*cm, 13*cm])
        tbl.setStyle(TableStyle([
            ('FONTSIZE',    (0, 0), (-1, -1), 9),
            ('TEXTCOLOR',   (0, 0), (0, -1), colors.grey),
            ('FONTNAME',    (0, 0), (0, -1), 'Helvetica-Bold'),
            ('VALIGN',      (0, 0), (-1, -1), 'TOP'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ]))
        story.append(tbl)

        if p.description or p.proposal_text:
            story.append(Spacer(1, 0.2*cm))
            story.append(Paragraph('<b>Beskrivelse</b>', label_style))
            tekst = (p.description or p.proposal_text or '').replace('\n', '<br/>')
            story.append(Paragraph(tekst, body_style))

        # Udfald
        story.append(Spacer(1, 0.2*cm))
        outcome_map = {'approved': 'Ja, godkendt', 'rejected': 'Ikke godkendt'}
        outcome_txt = outcome_map.get(p.outcome_status, 'Ikke angivet')
        story.append(Paragraph(f'<b>Godkendt:</b> {outcome_txt}', body_style))

        story.append(Paragraph('<b>Afgørelse</b>', label_style))
        story.append(Paragraph((p.decision or '—').replace('\n', '<br/>'), body_style))

        story.append(Paragraph('<b>Aftale</b>', label_style))
        story.append(Paragraph((p.agreement or '—').replace('\n', '<br/>'), body_style))

        # Bilag
        if p.attachments:
            story.append(Spacer(1, 0.15*cm))
            att_names = ', '.join(a.original_filename for a in p.attachments)
            story.append(Paragraph(f'<b>Bilag:</b> {att_names}', label_style))

        story.append(HRFlowable(width='100%', color=colors.lightgrey, thickness=0.5))

    doc.build(story, canvasmaker=_numbered_canvas_class())
    return buf.getvalue()
