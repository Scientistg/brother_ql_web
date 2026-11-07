#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
This is a web service to print labels on Brother QL label printers.
"""

import sys, logging, random, json, argparse, threading
from uuid import uuid4
from io import BytesIO
from urllib.parse import quote

from bottle import run, route, get, post, response, request, jinja2_view as view, static_file, redirect
from PIL import Image, ImageDraw, ImageFont

from brother_ql.devicedependent import models, label_type_specs, label_sizes
from brother_ql.devicedependent import ENDLESS_LABEL, DIE_CUT_LABEL, ROUND_DIE_CUT_LABEL
from brother_ql import BrotherQLRaster, create_label
from brother_ql.backends import backend_factory, guess_backend

from font_helpers import get_fonts

logger = logging.getLogger(__name__)

LABEL_SIZES = [ (name, label_type_specs[name]['name']) for name in label_sizes]

try:
    with open('config.json', encoding='utf-8') as fh:
        CONFIG = json.load(fh)
except FileNotFoundError as e:
    with open('config.example.json', encoding='utf-8') as fh:
        CONFIG = json.load(fh)

GROCY_PADDING_RIGHT = CONFIG["grocy_label"]["padding_right"]
GROCY_PADDING_AFTER_DM = CONFIG["grocy_label"]["padding_after_datamatrix"]
GROCY_PRODUCT_VERTICAL_TWEAK = CONFIG["grocy_label"]["product_vertical_tweak"]
GROCY_DATE_VERTICAL_GAP = CONFIG["grocy_label"]["date_vertical_gap"]
GROCY_CONTINUOUS_LENGTH_MM = CONFIG["grocy_label"].get("continuous_label_length_mm", 13)
GROCY_REVIEW_BEFORE_PRINT = CONFIG["grocy_label"].get("review_before_print", False)
PENDING_GROCY_LABELS = {}
PENDING_GROCY_LABELS_LOCK = threading.Lock()


@route('/')
def index():
    redirect('/labeldesigner')

@route('/static/<filename:path>')
def serve_static(filename):
    return static_file(filename, root='./static')

@route('/labeldesigner')
@view('labeldesigner.jinja2')
def labeldesigner():
    font_family_names = sorted(list(FONTS.keys()))
    return {'font_family_names': font_family_names,
            'fonts': FONTS,
            'label_sizes': LABEL_SIZES,
            'website': CONFIG['WEBSITE'],
            'label': CONFIG['LABEL']}

def get_label_context(request):
    """ might raise LookupError() """

    d = request.params.decode() # UTF-8 decoded form data

    font_family = d.get('font_family').rpartition('(')[0].strip()
    font_style  = d.get('font_family').rpartition('(')[2].rstrip(')')
    context = {
      'text':          d.get('text', None),
      'font_size': int(d.get('font_size', 100)),
      'font_family':   font_family,
      'font_style':    font_style,
      'label_size':    d.get('label_size', "62"),
      'kind':          label_type_specs[d.get('label_size', "62")]['kind'],
      'margin':    int(d.get('margin', 10)),
      'threshold': int(d.get('threshold', 70)),
      'align':         d.get('align', 'center'),
      'orientation':   d.get('orientation', 'standard'),
      'margin_top':    float(d.get('margin_top',    24))/100.,
      'margin_bottom': float(d.get('margin_bottom', 25))/100.,
      'margin_left':   float(d.get('margin_left',   35))/100.,
      'margin_right':  float(d.get('margin_right',  35))/100.,
      'grocycode': d.get('grocycode', None),
      'product': d.get('product', None),
      'duedate': d.get('due_date') or d.get('duedate')
    }
    context['margin_top']    = int(context['font_size']*context['margin_top'])
    context['margin_bottom'] = int(context['font_size']*context['margin_bottom'])
    context['margin_left']   = int(context['font_size']*context['margin_left'])
    context['margin_right']  = int(context['font_size']*context['margin_right'])

    context['fill_color']  = (255, 0, 0) if 'red' in context['label_size'] else (0, 0, 0)

    def get_font_path(font_family_name, font_style_name):
        try:
            if font_family_name is None or font_style_name is None:
                font_family_name = CONFIG['LABEL']['DEFAULT_FONTS']['family']
                font_style_name =  CONFIG['LABEL']['DEFAULT_FONTS']['style']
            font_path = FONTS[font_family_name][font_style_name]
        except KeyError:
            raise LookupError("Couln't find the font & style")
        return font_path

    context['font_path'] = get_font_path(context['font_family'], context['font_style'])

    def get_label_dimensions(label_size):
        try:
            ls = label_type_specs[context['label_size']]
        except KeyError:
            raise LookupError("Unknown label_size")
        return ls['dots_printable']

    width, height = get_label_dimensions(context['label_size'])
    if height > width: width, height = height, width
    if context['orientation'] == 'rotated': height, width = width, height
    context['width'], context['height'] = width, height

    return context

def create_label_im(text, **kwargs):
    label_type = kwargs['kind']
    im_font = ImageFont.truetype(kwargs['font_path'], kwargs['font_size'])
    im = Image.new('L', (20, 20), 'white')
    draw = ImageDraw.Draw(im)
    # workaround for a bug in multiline_textsize()
    # when there are empty lines in the text:
    lines = []
    for line in text.split('\n'):
        if line == '': line = ' '
        lines.append(line)
    text = '\n'.join(lines)
    # linesize = im_font.getsize(text)
    textsize = draw.multiline_textsize(text, font=im_font)
    width, height = kwargs['width'], kwargs['height']
    if kwargs['orientation'] == 'standard':
        if label_type in (ENDLESS_LABEL,):
            height = textsize[1] + kwargs['margin_top'] + kwargs['margin_bottom']
    elif kwargs['orientation'] == 'rotated':
        if label_type in (ENDLESS_LABEL,):
            width = textsize[0] + kwargs['margin_left'] + kwargs['margin_right']
    im = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(im)
    if kwargs['orientation'] == 'standard':
        if label_type in (DIE_CUT_LABEL, ROUND_DIE_CUT_LABEL):
            vertical_offset  = (height - textsize[1])//2
            vertical_offset += (kwargs['margin_top'] - kwargs['margin_bottom'])//2
        else:
            vertical_offset = kwargs['margin_top']
        horizontal_offset = max((width - textsize[0])//2, 0)
    elif kwargs['orientation'] == 'rotated':
        vertical_offset  = (height - textsize[1])//2
        vertical_offset += (kwargs['margin_top'] - kwargs['margin_bottom'])//2
        if label_type in (DIE_CUT_LABEL, ROUND_DIE_CUT_LABEL):
            horizontal_offset = max((width - textsize[0])//2, 0)
        else:
            horizontal_offset = kwargs['margin_left']
    offset = horizontal_offset, vertical_offset
    draw.multiline_text(offset, text, kwargs['fill_color'], font=im_font, align=kwargs['align'])
    return im

def autosize_font(text, font_path, max_width, max_height, starting_size):
    """Automatically shrink font size so the text fits inside max_width/max_height."""
    size = starting_size
    while size > 8:  # minimum readable size
        font = ImageFont.truetype(font_path, size)
        left, top, right, bottom = font.getbbox(text)
        w = right - left
        if w <= max_width and bottom <= max_height:
            return font
        size -= 2
    return ImageFont.truetype(font_path, 8)

def create_label_grocy(text, **kwargs):
    product = kwargs['product']
    duedate = kwargs['duedate']
    grocycode = kwargs['grocycode']

    # prepare grocycode datamatrix
    from pylibdmtx.pylibdmtx import encode
    encoded = encode(grocycode.encode('utf8'), size="SquareAuto")
    datamatrix = Image.frombytes('RGB', (encoded.width, encoded.height), encoded.pixels)

    label_width = kwargs['width']
    label_height = kwargs['height']
    if label_type_specs[kwargs['label_size']]['kind'] == ENDLESS_LABEL:
        layout_height = 200
        printable_width = max(label_width, label_height)
        length_dots = max(1, int(round(GROCY_CONTINUOUS_LENGTH_MM * 300 / 25.4)))
        width, height = printable_width, length_dots
        if kwargs['orientation'] == 'rotated':
            width, height = height, width
    else:
        layout_height = label_height
        width, height = label_width, label_height

    im = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(im)

    # --- datamatrix position ---
    vertical_offset = kwargs['margin_top']
    horizontal_offset = kwargs['margin_left']

    im.paste(
        datamatrix,
        (
            horizontal_offset,
            vertical_offset,
            horizontal_offset + encoded.width,
            vertical_offset + encoded.height
        )
    )

    # ================================
    #       AUTO-SIZE PRODUCT TEXT
    # ================================
    prod_x = horizontal_offset + encoded.width + GROCY_PADDING_AFTER_DM
    prod_y = vertical_offset + GROCY_PRODUCT_VERTICAL_TWEAK
    bottom_margin = max(0, kwargs['margin_bottom'])
    product_origin_y = max(0, prod_y)
    if duedate is not None:
        max_date_height = max(
            1,
            min(80, layout_height - product_origin_y - bottom_margin - GROCY_DATE_VERTICAL_GAP)
        )
        reserved_date_height = max_date_height + GROCY_DATE_VERTICAL_GAP
    else:
        max_date_height = 0
        reserved_date_height = 0

    max_product_width = max(
        1,
        width - horizontal_offset - encoded.width - GROCY_PADDING_AFTER_DM - GROCY_PADDING_RIGHT
    )
    max_product_height = max(
        1,
        min(120, layout_height - product_origin_y - bottom_margin - reserved_date_height)
    )

    product_font = autosize_font(
        product,
        kwargs['font_path'],
        max_product_width,
        max_product_height,
        starting_size=120
    )

    draw.text(
        (prod_x, prod_y),
        product,
        kwargs['fill_color'],
        font=product_font
    )

    # ================================
    #       AUTO-SIZE DUE DATE
    # ================================
    if duedate is not None:
        max_date_width = max(
            1,
            width - prod_x - GROCY_PADDING_RIGHT
        )

        product_bbox = product_font.getbbox(product)
        product_text_height = product_bbox[3] - product_bbox[1]
        date_x = prod_x
        date_y = prod_y + product_text_height + GROCY_DATE_VERTICAL_GAP
        max_date_height = max(
            1,
            min(max_date_height, height - date_y)
        )
        date_font = autosize_font(
            duedate,
            kwargs['font_path'],
            max_date_width,
            max_date_height,
            starting_size=80
        )

        draw.text(
            (date_x, date_y),
            duedate,
            kwargs['fill_color'],
            font=date_font
        )

    return im


def build_grocy_print_data(context):
    im = create_label_grocy(**context)
    if context['kind'] == ENDLESS_LABEL:
        rotate = 0 if context['orientation'] == 'standard' else 90
    elif context['kind'] in (ROUND_DIE_CUT_LABEL, DIE_CUT_LABEL):
        rotate = 'auto'

    qlr = BrotherQLRaster(CONFIG['PRINTER']['MODEL'])
    red = 'red' in context['label_size']
    create_label(
        qlr,
        im,
        context['label_size'],
        red=red,
        threshold=context['threshold'],
        cut=True,
        rotate=rotate
    )

    preview_im = im
    if context['kind'] == ENDLESS_LABEL and rotate not in ('auto', 0):
        preview_im = im.rotate(rotate, expand=True)
    elif context['kind'] in (ROUND_DIE_CUT_LABEL, DIE_CUT_LABEL) and rotate == 'auto':
        label_width, label_height = label_type_specs[context['label_size']]['dots_printable']
        if im.size == (label_height, label_width):
            preview_im = im.rotate(90, expand=True)

    return preview_im, qlr.data


def send_to_printer(data):
    be = BACKEND_CLASS(CONFIG['PRINTER']['PRINTER'])
    try:
        be.write(data)
    finally:
        be.dispose()


def grocy_job_details(job):
    width_mm = job['image'].width * 25.4 / 300
    height_mm = job['image'].height * 25.4 / 300
    return {
        'id': job['id'],
        'product': job['product'],
        'duedate': job['duedate'],
        'label_size': job['label_size'],
        'width': job['image'].width,
        'height': job['image'].height,
        'width_mm': round(width_mm, 1),
        'height_mm': round(height_mm, 1)
    }


@get('/grocy/review')
@view('grocy_review.jinja2')
def grocy_review():
    with PENDING_GROCY_LABELS_LOCK:
        jobs = [grocy_job_details(job) for job in PENDING_GROCY_LABELS.values()]
    return {
        'website': CONFIG['WEBSITE'],
        'jobs': jobs,
        'message': request.query.get('message')
    }


@get('/api/grocy/review/<job_id>/preview')
def preview_grocy_job(job_id):
    with PENDING_GROCY_LABELS_LOCK:
        job = PENDING_GROCY_LABELS.get(job_id)
        if job is None:
            response.status = 404
            return 'Pending label not found'
        image_data = job['image_data']
    response.set_header('Content-type', 'image/png')
    return image_data


@post('/grocy/review/<job_id>/print')
def print_grocy_job(job_id):
    with PENDING_GROCY_LABELS_LOCK:
        job = PENDING_GROCY_LABELS.get(job_id)
        if job is None:
            return redirect('/grocy/review?message=Pending%20label%20not%20found')
        if job['printing']:
            return redirect('/grocy/review?message=This%20label%20is%20already%20printing')
        if DEBUG:
            return redirect('/grocy/review?message=Debug%20mode%20is%20preventing%20printing')
        job['printing'] = True

    try:
        send_to_printer(job['raster_data'])
    except Exception as e:
        with PENDING_GROCY_LABELS_LOCK:
            job['printing'] = False
        logger.warning('Exception happened: %s', e)
        return redirect('/grocy/review?message=' + quote('Printing failed: {}'.format(e)))

    with PENDING_GROCY_LABELS_LOCK:
        PENDING_GROCY_LABELS.pop(job_id, None)
    return redirect('/grocy/review?message=Label%20printed')


@post('/grocy/review/<job_id>/discard')
def discard_grocy_job(job_id):
    with PENDING_GROCY_LABELS_LOCK:
        job = PENDING_GROCY_LABELS.get(job_id)
        if job is not None and job['printing']:
            return redirect('/grocy/review?message=This%20label%20is%20already%20printing')
        PENDING_GROCY_LABELS.pop(job_id, None)
    return redirect('/grocy/review?message=Label%20discarded')


@get('/api/preview/text')
@post('/api/preview/text')
def get_preview_image():
    context = get_label_context(request)
    im = create_label_im(**context)
    return_format = request.query.get('return_format', 'png')
    if return_format == 'base64':
        import base64
        response.set_header('Content-type', 'text/plain')
        return base64.b64encode(image_to_png_bytes(im))
    else:
        response.set_header('Content-type', 'image/png')
        return image_to_png_bytes(im)

def image_to_png_bytes(im):
    image_buffer = BytesIO()
    im.save(image_buffer, format="PNG")
    image_buffer.seek(0)
    return image_buffer.read()

@post('/api/print/grocy')
@get('/api/print/grocy')
def print_grocy():
    """
    API endpoint to consume the grocy label webhook.

    returns; JSON
    """

    logger.debug("Grocy label request payload: %r", dict(request.params.decode()))
    return_dict = {'success' : False }

    try:
        context = get_label_context(request)
    except LookupError as e:
        return_dict['error'] = e.msg
        return return_dict

    if context['product'] is None:
        return_dict['error'] = 'Please provide the product for the label'
        return return_dict

    im, raster_data = build_grocy_print_data(context)
    if DEBUG:
        im.save('sample-out.png')

    if GROCY_REVIEW_BEFORE_PRINT:
        job_id = uuid4().hex
        job = {
            'id': job_id,
            'product': context['product'],
            'duedate': context['duedate'],
            'label_size': context['label_size'],
            'image': im,
            'image_data': image_to_png_bytes(im),
            'raster_data': raster_data,
            'printing': False
        }
        with PENDING_GROCY_LABELS_LOCK:
            PENDING_GROCY_LABELS[job_id] = job
        return {
            'success': True,
            'queued': True,
            'review_url': '/grocy/review'
        }

    if not DEBUG:
        try:
            send_to_printer(raster_data)
        except Exception as e:
            return_dict['message'] = str(e)
            logger.warning('Exception happened: %s', e)
            return return_dict

    return_dict['success'] = True
    if DEBUG: return_dict['data'] = str(raster_data)
    return return_dict

@post('/api/print/text')
@get('/api/print/text')
def print_text():
    """
    API to print a label

    returns: JSON

    Ideas for additional URL parameters:
    - alignment
    """

    return_dict = {'success': False}

    try:
        context = get_label_context(request)
    except LookupError as e:
        return_dict['error'] = e.msg
        return return_dict

    if context['text'] is None:
        return_dict['error'] = 'Please provide the text for the label'
        return return_dict

    im = create_label_im(**context)
    if DEBUG: im.save('sample-out.png')

    if context['kind'] == ENDLESS_LABEL:
        rotate = 0 if context['orientation'] == 'standard' else 90
    elif context['kind'] in (ROUND_DIE_CUT_LABEL, DIE_CUT_LABEL):
        rotate = 'auto'

    qlr = BrotherQLRaster(CONFIG['PRINTER']['MODEL'])
    red = False
    if 'red' in context['label_size']:
        red = True
    create_label(qlr, im, context['label_size'], red=red, threshold=context['threshold'], cut=True, rotate=rotate)

    if not DEBUG:
        try:
            be = BACKEND_CLASS(CONFIG['PRINTER']['PRINTER'])
            be.write(qlr.data)
            be.dispose()
            del be
        except Exception as e:
            return_dict['message'] = str(e)
            logger.warning('Exception happened: %s', e)
            return return_dict

    return_dict['success'] = True
    if DEBUG: return_dict['data'] = str(qlr.data)
    return return_dict

def main():
    global DEBUG, FONTS, BACKEND_CLASS, CONFIG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default=False)
    parser.add_argument('--loglevel', type=lambda x: getattr(logging, x.upper()), default=False)
    parser.add_argument('--font-folder', default=False, help='folder for additional .ttf/.otf fonts')
    parser.add_argument('--default-label-size', default=False, help='Label size inserted in your printer. Defaults to 62.')
    parser.add_argument('--default-orientation', default=False, choices=('standard', 'rotated'), help='Label orientation, defaults to "standard". To turn your text by 90°, state "rotated".')
    parser.add_argument('--model', default=False, choices=models, help='The model of your printer (default: QL-500)')
    parser.add_argument('printer',  nargs='?', default=False, help='String descriptor for the printer to use (like tcp://192.168.0.23:9100 or file:///dev/usb/lp0)')
    args = parser.parse_args()

    if args.printer:
        CONFIG['PRINTER']['PRINTER'] = args.printer

    if args.port:
        PORT = args.port
    else:
        PORT = CONFIG['SERVER']['PORT']

    if args.loglevel:
        LOGLEVEL = args.loglevel
    else:
        LOGLEVEL = CONFIG['SERVER']['LOGLEVEL']

    if LOGLEVEL == 'DEBUG':
        DEBUG = True
    else:
        DEBUG = False

    if args.model:
        CONFIG['PRINTER']['MODEL'] = args.model

    if args.default_label_size:
        CONFIG['LABEL']['DEFAULT_SIZE'] = args.default_label_size

    if args.default_orientation:
        CONFIG['LABEL']['DEFAULT_ORIENTATION'] = args.default_orientation

    if args.font_folder:
        ADDITIONAL_FONT_FOLDER = args.font_folder
    else:
        ADDITIONAL_FONT_FOLDER = CONFIG['SERVER']['ADDITIONAL_FONT_FOLDER']


    logging.basicConfig(level=LOGLEVEL)

    try:
        selected_backend = guess_backend(CONFIG['PRINTER']['PRINTER'])
    except ValueError:
        parser.error("Couln't guess the backend to use from the printer string descriptor")
    BACKEND_CLASS = backend_factory(selected_backend)['backend_class']

    if CONFIG['LABEL']['DEFAULT_SIZE'] not in label_sizes:
        parser.error("Invalid --default-label-size. Please choose on of the following:\n:" + " ".join(label_sizes))

    FONTS = get_fonts()
    if ADDITIONAL_FONT_FOLDER:
        FONTS.update(get_fonts(ADDITIONAL_FONT_FOLDER))

    if not FONTS:
        sys.stderr.write("Not a single font was found on your system. Please install some or use the \"--font-folder\" argument.\n")
        sys.exit(2)

    for font in CONFIG['LABEL']['DEFAULT_FONTS']:
        try:
            FONTS[font['family']][font['style']]
            CONFIG['LABEL']['DEFAULT_FONTS'] = font
            logger.debug("Selected the following default font: {}".format(font))
            break
        except: pass
    if CONFIG['LABEL']['DEFAULT_FONTS'] is None:
        sys.stderr.write('Could not find any of the default fonts. Choosing a random one.\n')
        family =  random.choice(list(FONTS.keys()))
        style =   random.choice(list(FONTS[family].keys()))
        CONFIG['LABEL']['DEFAULT_FONTS'] = {'family': family, 'style': style}
        sys.stderr.write('The default font is now set to: {family} ({style})\n'.format(**CONFIG['LABEL']['DEFAULT_FONTS']))

    run(host=CONFIG['SERVER']['HOST'], port=PORT, debug=DEBUG)

if __name__ == "__main__":
    main()
