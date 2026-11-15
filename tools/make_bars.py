"""Generate shared, compact bar frames and the BoostDeck RHMI screens."""
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from PIL import Image, ImageDraw

import shift_art as art

ROOT = Path(__file__).resolve().parents[1]
BARS = (
    ('Boost', 'psi', 0, 35), ('AFR', '', 9, 19), ('Timing', 'deg', -20, 50),
    ('Intake air', r'\260F', -20, 100), ('Transmission', r'\260F', 0, 150),
    ('Rail pressure', 'psi', 0, 250), ('Oil pressure', 'psi', 0, 8), ('Coolant', r'\260F', 0, 130),
)
COLORS = (
    ('BMW Orange', '#ff4f1a'), ('Blue', '#58ceff'), ('Green', '#6cdb93'), ('White', '#e2eaf0'),
    ('Purple', '#ad72ff'), ('Pink', '#ff6cba'), ('Cyan', '#20f1e6'), ('Red', '#ff4545'),
)
MULTICOLOR = ('#58ceff', '#5de0bf', '#cbacff', '#ffbe70',
              '#6cdb93', '#60c5ff', '#ffb76a', '#6fd9ba')
THEMES = tuple(name for name, _ in COLORS) + ('Multicolor',)
# The Temperature screen's choices, in the order of their actions (70, 71). The readings are kept in Celsius.
TEMPERATURE_UNITS = (('Celsius', r'\260C'), ('Fahrenheit', r'\260F'))
STEPS, FIRST = 32, 55000

# The gauge page: one card per reading in a 4 x 2 grid. The cards and the names are drawn in one picture (CARDS); a
# label shows each value and a bar its level. The engine readings take the top row, the temperatures and oil the
# bottom one. CARD_ORDER lists BARS indices by grid position, left to right, then the second row.
CARDS = 55590
CARD_ORDER = (0, 1, 2, 5, 3, 4, 6, 7)
GRID_X, GRID_Y, GRID_W, GRID_H, GAP = 24, 13, 1172, 400, 12
CARD_W, CARD_H = (GRID_W - 3*GAP) // 4, (GRID_H - GAP) // 2
BAR_W, BAR_H = 252, 14
CARD_FILL, CARD_EDGE, NAME_COLOR, TRACK = '#18202a', '#2a3644', '#90a4ba', '#263342'

def card_origin(position):
    """The top left corner of the card at a grid position, in screen coordinates."""
    return GRID_X + (position % 4)*(CARD_W + GAP), GRID_Y + (position // 4)*(CARD_H + GAP)

def card_picture():
    font = art.font(26)
    image = Image.new('RGBA', (GRID_W, GRID_H))
    draw = ImageDraw.Draw(image)
    for position, index in enumerate(CARD_ORDER):
        x, y = card_origin(position)
        x, y = x - GRID_X, y - GRID_Y
        draw.rounded_rectangle((x, y, x+CARD_W-1, y+CARD_H-1), radius=12, fill=CARD_FILL, outline=CARD_EDGE, width=2)
        draw.text((x+16, y+14), BARS[index][0].upper(), font=font, fill=NAME_COLOR)
    return image

def add_props(component, x, y, w, h, image=False):
    props = ET.SubElement(component, 'properties')
    for prop, value in [(20,x), (21,y), (9,w), (10,h)] + ([(2,'false'),(3,'true')] if image else []):
        ET.SubElement(props, 'property', id=str(prop), value=str(value))

def label(parent, identifier, model):
    return ET.SubElement(parent, 'label', id=str(identifier), model=str(model))

def button(parent, identifier, action, text):
    ET.SubElement(parent,'button',id=str(identifier),action=str(action),text=text)

def build_assets():
    def store(archive, identifier, image):
        data = BytesIO()
        image.save(data,format='PNG',optimize=True)
        archive.writestr(ZipInfo(f'{identifier}.png'),data.getvalue(),ZIP_DEFLATED)

    with ZipFile(ROOT/'plugin/BoostDeck_ALL_Images.zip','w', compression=ZIP_DEFLATED) as archive:
        for color, fill in enumerate(tuple(fill for _, fill in COLORS) + MULTICOLOR):
            for step in range(STEPS+1):
                if color and not step:
                    continue
                image = Image.new('RGBA',(BAR_W,BAR_H))
                draw = ImageDraw.Draw(image)
                draw.rounded_rectangle((0,0,BAR_W-1,BAR_H-1),radius=BAR_H//2,fill=TRACK)
                if step:
                    width = max(BAR_H, round(BAR_W*step/STEPS))
                    draw.rounded_rectangle((0,0,width-1,BAR_H-1),radius=BAR_H//2,fill=fill)
                store(archive, FIRST+color*STEPS+step, image)
        store(archive, CARDS, card_picture())

def main():
    root = ET.Element('pluginApps')
    app = ET.SubElement(root,'pluginApp',applicationType='VehicleInformation',applicationWeight='210',text='BoostDeck')
    entry = ET.SubElement(app,'entryButton',id='1',action='10')
    ET.SubElement(ET.SubElement(entry,'properties'),'property',id='23',value='2')
    actions = ET.SubElement(app,'actions')
    for identifier, target in ((10,100),(31,104),(32,105)):
        ET.SubElement(actions,'hmiAction',id=str(identifier),target=str(target))
    for identifier in range(40,40+len(THEMES)):
        ET.SubElement(actions,'raAction',id=str(identifier))
    for identifier in range(70,70+len(TEMPERATURE_UNITS)):
        ET.SubElement(actions,'raAction',id=str(identifier))
    models = ET.SubElement(app,'models')
    states = ET.SubElement(app,'hmiStates')
    gauges = ET.SubElement(states,'hmiState',id='100',text='BoostDeck')
    options = ET.SubElement(gauges,'optionComponents')
    button(options,33,31,'Gauge color')
    button(options,34,32,'Temperature')
    content = ET.SubElement(gauges,'components')
    # The cards first, so the values and bars draw over them.
    ET.SubElement(models,'imageIdModel',id='69',imageId=str(CARDS))
    add_props(ET.SubElement(content,'image',id='209',model='69'),GRID_X,GRID_Y,GRID_W,GRID_H,True)
    for i in range(len(BARS)):
        ET.SubElement(models,'raDataModel',id=str(50+i),modelType='Richtext')
        ET.SubElement(models,'imageIdModel',id=str(60+i),imageId=str(FIRST))
        bar = ET.SubElement(content,'image',id=str(200+i),model=str(60+i))
        reading = label(content,150+i,50+i)
        x,y = card_origin(CARD_ORDER.index(i))
        add_props(reading,x+14,y+58,CARD_W-28,66)
        add_props(bar,x+16,y+CARD_H-40,BAR_W,BAR_H,True)
    ET.SubElement(models,'raDataModel',id='110',modelType='Richtext')
    color_screen = ET.SubElement(states,'hmiState',id='104',parentId='100',text='Gauge color')
    content = ET.SubElement(color_screen,'components')
    label(content,389,110)
    for i,name in enumerate(THEMES):
        button(content,390+i,40+i,name)
    ET.SubElement(models,'raDataModel',id='111',modelType='Richtext')
    unit_screen = ET.SubElement(states,'hmiState',id='105',parentId='100',text='Temperature')
    content = ET.SubElement(unit_screen,'components')
    label(content,409,111)
    for i,(name,_) in enumerate(TEMPERATURE_UNITS):
        button(content,410+i,70+i,name)
    ET.indent(root,space='  ')
    # LF on every system: tools/package_release.py refuses Windows line endings.
    with open(ROOT/'plugin/BoostDeck.xml','w',encoding='utf-8',newline='\n') as out:
        ET.ElementTree(root).write(out,encoding='unicode',xml_declaration=True)
    scales = [f'    {{{lo}, {hi}, "{name}", "{unit}"}}' for name,unit,lo,hi in BARS]
    colors = ', '.join(f'"{name}"' for name in THEMES)
    units = ', '.join(f'{{"{name}", "{unit}"}}' for name,unit in TEMPERATURE_UNITS)
    (ROOT/'src/dial_scales.h').write_text(
        '// Generated by tools/make_bars.py.\n'
        f'static const unsigned kDialSteps = {STEPS};\n'
        f'static const unsigned kBarFirst = {FIRST};\n'
        f'static const unsigned kMulticolor = {len(COLORS)};\n'
        f'static const char *const kColorNames[] = {{{colors}}};\n'
        'static const struct TemperatureUnit { const char *name, *unit; } kTemperatureUnits[] = '
        f'{{{units}}};\n'
        'static const struct DialScale { double minimum, maximum; const char *name, *unit; } kDialScales[] = {\n'
        + ',\n'.join(scales) + '\n};\n', newline='\n')
    build_assets()

if __name__ == '__main__':
    main()
