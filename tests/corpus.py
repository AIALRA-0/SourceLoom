"""Twelve original synthetic seeds; format variants are not independent sources."""
from io import BytesIO
import html
import zipfile
import textwrap
from reportlab.pdfgen import canvas

SEEDS=[
 ('algebra','For this example x is positive. Dividing by x is not valid when x equals zero.'),
 ('statistics','The synthetic sample has 20 items. A correlation of 0.5 does not establish a causal effect.'),
 ('physics','Assume constant speed of 3 metres per second for 4 seconds. Acceleration is excluded.'),
 ('chemistry','This imaginary sample contains 2 units of A and 3 units of B. Do not infer a real reaction.'),
 ('biology','The simulated group contains 12 cells. Only 8 cells pass the stated observation filter.'),
 ('computing','The example takes 100 milliseconds with 20 fixed and 80 variable. Ignore startup only in this example.'),
 ('networks','Under the given simulation 3 packets are delayed. A delay does not imply permanent packet loss.'),
 ('economics','The fictional price rises from 10 to 12 units. This scenario does not predict a market price.'),
 ('history','Source A reports an event in year 1200. Source B disputes the date; neither is treated as verified here.'),
 ('linguistics','In this invented language the suffix ka marks plurality. The exception word tika is singular.'),
 ('medical-methods','This is fictional teaching data with 6 records, not clinical advice. Missing records are not negative results.'),
 ('legal-methods','In this fictional rule only category A qualifies. The exception in clause 3 overrides the general example.')]


def sample(domain,text,format):
    full=f'Synthetic source: {domain}\n\n{text}\n\nTail exception: all numbers are teaching assumptions.'
    if format=='txt':return full.encode()
    if format=='md':return ('# '+domain+'\n\n'+text+'\n\n> Tail exception: all numbers are teaching assumptions.').encode()
    if format=='html':return ('<h1>'+domain+'</h1><p>'+html.escape(text)+'</p><p>Tail exception: all numbers are teaching assumptions.</p>').encode()
    if format=='pdf':
        buf=BytesIO();pdf=canvas.Canvas(buf)
        for i,line in enumerate(full.splitlines()):
            for j,chunk in enumerate(textwrap.wrap(line,width=82)):pdf.drawString(35,790-i*35-j*13,chunk)
        pdf.save();return buf.getvalue()
    buf=BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        paras=''.join('<w:p><w:r><w:t>'+html.escape(s)+'</w:t></w:r></w:p>' for s in full.split('\n\n'))
        z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+paras+'</w:body></w:document>')
    return buf.getvalue()
