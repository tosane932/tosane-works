"""Offline A4 build. Build-only dependencies; never imports the application."""
from pathlib import Path
import argparse
import hashlib
import re
from html import escape
from tempfile import TemporaryDirectory
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.platypus import (BaseDocTemplate, PageTemplate, Frame, Paragraph,
                              Spacer, PageBreak, Table, TableStyle, KeepTogether, Preformatted)
from reportlab.platypus.tableofcontents import TableOfContents
from fontTools.ttLib import TTFont as Font
from fontTools.varLib.instancer import instantiateVariableFont

ROOT = Path(__file__).resolve().parents[2]
FONT_SHA = 'c2f3b4d463500a2ddcd3849cded1fceeb9fd6d1c32e6cbecd568453ba50fc68f'

class Guide(BaseDocTemplate):
    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name == 'chapter':
            title = flowable.getPlainText()
            key = 'chapter-' + str(self.seq.nextf('chapter'))
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(title, key, 0)
            self.notify('TOCEntry', (0, title, self.page, key))

def build(font_path, output):
    font_path = Path(font_path)
    if hashlib.sha256(font_path.read_bytes()).hexdigest() != FONT_SHA:
        raise ValueError('Unexpected font; use the pinned Noto Sans JP source in README')
    with TemporaryDirectory() as temp:
        font = Font(font_path)
        instantiateVariableFont(font, {'wght': 400}, inplace=True)
        static_font = Path(temp) / 'regular.ttf'
        font.save(static_font)
        pdfmetrics.registerFont(TTFont('GuideJP', str(static_font)))
        base = dict(fontName='GuideJP', fontSize=10.5, leading=18, wordWrap='CJK', spaceAfter=9)
        styles = {name: ParagraphStyle(name, **(base | values)) for name, values in {
            'body':{}, 'title':{'fontSize':26,'leading':40,'spaceAfter':22},
            'chapter':{'fontSize':19,'leading':29,'spaceAfter':18,'keepWithNext':True},
            'heading':{'fontSize':13,'leading':23,'spaceBefore':10,'keepWithNext':True},
            'minor':{'fontSize':11,'leading':20,'spaceBefore':8,'keepWithNext':True},
            'cell':{'fontSize':9,'leading':15,'spaceAfter':0},
            'code':{'fontSize':9,'leading':15,'leftIndent':10,'backColor':colors.HexColor('#f2f2f2')},
        }.items()}
        def p(text, style='body'):
            return Paragraph(escape(text).replace('\n','<br/>'), styles[style])
        story=[]
        source=(ROOT/'docs/system-guide/guide.md').read_text()+ '\n\n'+(ROOT/'docs/system-guide/legacy-reference.md').read_text()
        blocks=re.split(r'\n\s*\n', source.strip())
        for index, block in enumerate(blocks):
            if block.startswith('# '):
                story.extend([Spacer(1,95),p(block[2:],'title')]);continue
            if block.startswith('## '):
                if not any(isinstance(x,TableOfContents) for x in story):
                    story.extend([PageBreak(), p('目次','heading')])
                    toc=TableOfContents();toc.levelStyles=[ParagraphStyle('toc',**(base|{'leftIndent':0,'spaceBefore':9,'fontSize':11}))]
                    story.append(toc)
                story.extend([PageBreak(),p(block[3:],'chapter')]);continue
            if block.startswith('### '): story.append(p(block[4:],'heading'));continue
            if block.startswith('#### '): story.append(p(block[5:],'minor'));continue
            if block.startswith('```'):
                story.append(KeepTogether([Preformatted('\n'.join(block.splitlines()[1:-1]),styles['code'])]));continue
            if block.startswith('|'):
                rows=[]
                for line in block.splitlines():
                    cells=[s.strip() for s in line.strip('|').split('|')]
                    if all(re.fullmatch(r'[-: ]+',c) for c in cells):continue
                    rows.append([p(c,'cell') for c in cells])
                widths=[(A4[0]-100)/len(rows[0])]*len(rows[0])
                t=Table(rows,colWidths=widths,repeatRows=1,hAlign='LEFT')
                t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('GRID',(0,0),(-1,-1),.5,colors.grey),('BACKGROUND',(0,0),(-1,0),colors.HexColor('#eeeeee')),('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7)]))
                story.extend([t,Spacer(1,12)]);continue
            if block.startswith(('ブラウザ（HTML', '商品選択 →')):
                rows=[[p(line,'cell')] for line in block.splitlines()]
                diagram=Table(rows,colWidths=[A4[0]-100])
                diagram.setStyle(TableStyle([('BOX',(0,0),(-1,-1),.7,colors.grey),('LINEBELOW',(0,0),(-1,-2),.3,colors.lightgrey),('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#f7f7f7')),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
                story.extend([KeepTogether([diagram]),Spacer(1,12)]);continue
            story.append(p(block))
        def footer(c,doc):
            c.saveState();c.setFont('GuideJP',8);c.setFillColor(colors.HexColor('#444444'))
            c.drawString(50,29,'Bakery Hub | システム教科書 | 2026-11-04 / 6f61e12')
            c.drawRightString(A4[0]-50,29,str(doc.page));c.restoreState()
        output.parent.mkdir(parents=True,exist_ok=True)
        doc=Guide(str(output),pagesize=A4,leftMargin=50,rightMargin=50,topMargin=48,bottomMargin=52,title='Bakery Hub システム教科書',author='Bakery Hub')
        doc.addPageTemplates(PageTemplate(id='guide',frames=[Frame(50,52,A4[0]-100,A4[1]-100,id='body',leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)],onPage=footer))
        doc.multiBuild(story,canvasmaker=lambda *a,**k:canvas.Canvas(*a,**(k|{'invariant':1})))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--font',required=True,type=Path);parser.add_argument('--output',type=Path,default=ROOT/'static/docs/bakery-hub-system-guide.pdf');args=parser.parse_args();build(args.font,args.output)
