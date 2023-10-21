# -*- coding: utf-8 -*-

import os
import re
import sys
import tempfile
from pathlib import Path
from ghostscript import Ghostscript
import PyPDF2
from PIL import Image, ImageDraw
import colorsys
import numpy as np
import shutil
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from autoeq.frequency_response import FrequencyResponse
sys.path.insert(1, os.path.realpath(os.path.join(sys.path[0], os.pardir, os.pardir)))
from measurements.name_index import NameIndex, NameItem
from measurements.crawler import Crawler
from measurements.image_graph_parser import ImageGraphParser

DIR_PATH = Path(__file__).parent


class Oratory1990Crawler(Crawler):
    def __init__(self, driver=None, delete_existing_on_prompt=True, redownload=False):
        if driver is None:
            opts = Options()
            opts.add_argument('--headless')
            driver = webdriver.Chrome(options=opts)
        super().__init__(driver=driver, delete_existing_on_prompt=delete_existing_on_prompt, redownload=redownload)

    @staticmethod
    def read_name_index():
        return NameIndex.read_tsv(os.path.join(DIR_PATH, 'name_index.tsv'))

    def write_name_index(self):
        self.name_index.write_tsv(os.path.join(DIR_PATH, 'name_index.tsv'))

    def guess_name(self, item):
        """Tries to guess what the name might be."""
        return item.source_name

    @staticmethod
    def image_path(item):
        return DIR_PATH.joinpath('images', f'{item.name}.png')

    def parse_pdf(self, item):
        pdf_path = self.pdf_path(item)
        image_path = self.image_path(item)
        with open(pdf_path, 'rb') as fh:
            text = PyPDF2.PdfReader(fh).pages[0].extract_text()
        # Convert to image with ghostscript
        # Using temporary paths with Ghostscript because it seems to be unable to work with non-ascii characters
        tmp_in = Path(tempfile.gettempdir()).joinpath('__tmp.pdf')
        tmp_out = Path(tempfile.gettempdir()).joinpath('__tmp.png')
        shutil.copy(pdf_path, tmp_in)
        Ghostscript(
            b'pdf2png', b'-dNOPAUSE', b'-sDEVICE=png16m', b'-dBATCH', b'-r600', b'-dUseCropBox',
            f'-sOutputFile={tmp_out}'.encode('utf-8'), tmp_in.encode('utf-8')).exit()
        shutil.copy(tmp_out, image_path)
        return text

    @staticmethod
    def pdf_path(item):
        return DIR_PATH.joinpath('pdf', f'{item.name}.pdf')

    def resolve(self, item):
        true_item = self.name_index.find_one(source_name=item.source_name)
        if true_item is not None and true_item.name is not None:
            item.name = true_item.name
        pdf_path = self.pdf_path(item)
        super().resolve(item)
        if not self.pdf_path(item).exists():
            self.download(item.url, pdf_path)
        with open(pdf_path, 'rb') as fh:
            text = PyPDF2.PdfReader(fh).pages[0].extract_text()
        if re.search(r'(?:measured|measurements?) (?:by|from)', text, flags=re.IGNORECASE):
            # Ignore stuff done based on Crinacle's measurements
            item.form = 'ignore'
        elif 'measured on' in text.lower():
            ix = text.lower().index('measured on')
            ix += len('measured on ')
            item.rig = text[ix:].split(' ')[0]
        else:
            print('No measured on in PDF for:', item)

    def is_prompt_needed(self, item):
        if item.form == 'ignore':
            return False
        return item.name is None or item.form is None or item.rig is None

    def crawl(self):
        if self.driver is None:
            raise TypeError('self.driver cannot be None')
        self.name_index = self.read_name_index()
        document = self.get_beautiful_soup('https://www.reddit.com/r/oratory1990/wiki/index/list_of_presets')
        table_header = document.find(id='wiki_full_list_of_eq_settings.3A')
        if table_header is None:
            raise RedditCrawlFailed('Failed to parse Reddit page. Maybe try again?')
        self.crawl_index = NameIndex()
        manufacturer, model = None, None
        for row in table_header.parent.find('table').find('tbody').find_all('tr'):
            cells = row.find_all('td')
            # Parse cells
            # Try to read manufacturer from the first cell and if it fails (cell is empty), use the previous name
            manufacturer = cells[0].text.strip() if cells[0].text.strip() != '-' else manufacturer
            # Try to read model from the second cell and if it fails (cell is empty), use the previous name
            model = cells[1].text.strip() if cells[1].text.strip() != '-' else model
            source_name = f'{manufacturer} {model}'
            # Third cell contains hyperlink, where the anchor is the PDF and text is target name
            url = cells[2].find('a')['href'].replace('?dl=0', '?dl=1')
            form = 'over-ear' if 'over-ear' in cells[2].text.strip().lower() else 'in-ear'
            # Fourth cell is notes
            notes = cells[3].text.strip()
            if 'preliminary' in notes.lower() or ' EQ' in notes:
                continue  # Skip various EQ settings and preliminary measurements
            if notes and notes.lower() != 'standard':
                source_name += f' ({notes})'
            item = NameItem(source_name, None, form, url=url)
            known_item = self.name_index.find_one(source_name=source_name)  # TODO: Switch to URL
            if known_item is not None:
                if known_item.name is not None:
                    item.name = known_item.name
                if known_item.form is not None:
                    item.form = known_item.form
                if known_item.rig is not None:
                    item.rig = known_item.rig
            if not self.crawl_index.find(source_name=source_name):
                self.crawl_index.add(item)
        return self.crawl_index

    @staticmethod
    def parse_image(im, model, px_top=800, px_bottom=4400, px_left=0, px_right=2500):
        """Parses graph image downloaded from innerfidelity.com"""
        # Crop out everything but graph area (roughly)
        box = (px_left, px_top, im.size[0] - px_right, im.size[1] - px_bottom)
        im = im.crop(box)
        # im.show()

        # Find graph edges
        v_lines = ImageGraphParser.find_lines(im, 'vertical')
        h_lines = ImageGraphParser.find_lines(im, 'horizontal')

        # Crop by graph edges
        try:
            box = (v_lines[0], h_lines[0], v_lines[1], h_lines[1])
        except IndexError as err:
            raise GraphParseFailed('Failed to parse PDF')
        im = im.crop(box)
        # im.show()

        # X axis
        f_min = 10
        f_max = 20000
        f_step = (f_max / f_min) ** (1 / im.size[0])
        f = [f_min]
        for _ in range(1, im.size[0]):
            f.append(f[-1] * f_step)

        # Y axis
        a_max = 30
        a_min = -20
        a_res = (a_max - a_min) / im.size[1]

        _im = im.copy()
        pix = _im.load()
        amplitude = []
        y_legend = 40 / 50 * im.size[1]
        x0_legend = np.log(70 / f_min) / np.log(f_step)
        x1_legend = np.log(1000 / f_min) / np.log(f_step)
        # Iterate each column
        for x in range(im.size[0]):
            pxs = []  # Graph pixels
            # Iterate each row (pixel in column)
            for y in range(im.size[1]):
                if y > y_legend and x0_legend < x < x1_legend:
                    # Skip pixels in the legend box
                    continue

                # Convert read RGB pixel values and convert to HSV
                h, s, v = colorsys.rgb_to_hsv(*[v / 255.0 for v in im.getpixel((x, y))])
                # Graph pixels are colored
                if 0.7 < s < 0.9 and 20 / 360 < h < 30 / 360:
                    pxs.append(float(y))
                else:
                    p = im.getpixel((x, y))
                    pix[x, y] = (int(0.3 * p[0]), int(255 * 0.7 + 0.3 * p[1]), int(0.3 * p[2]))
            if not pxs:
                # No graph pixels found on this column
                amplitude.append(None)
            else:
                # Mean of recorded pixels
                v = np.mean(pxs)
                # Convert to dB value
                v = a_max - v * a_res
                amplitude.append(v)

        # Inspection image
        draw = ImageDraw.Draw(_im)
        x0 = np.log(20 / f_min) / np.log(f_step)
        x1 = np.log(10000 / f_min) / np.log(f_step)
        draw.rectangle(((x0, 10 / a_res), (x1, 40 / a_res)), outline='magenta')
        draw.rectangle(((x0 + 1, 10 / a_res + 1), (x1 - 1, 40 / a_res - 1)), outline='magenta')

        fr = FrequencyResponse(model, f, amplitude)
        fr.interpolate()
        fr.center()
        return fr, _im

    def target_group_key(self, item):
        """Key for grouping measurements (NameItems) that should be averaged"""
        return f'{item.form}/{item.name}'

    def target_path(self, item):
        """Target file path for the item in measurements directory"""
        if item.form is None or item.name is None:
            return None
        return DIR_PATH.joinpath('data', item.form, f'{item.name}.csv')

    def process_group(self, items, new_only=True):
        if items.form == 'ignore':
            return

        pdf_dir = os.path.join(DIR_PATH, 'pdf')
        image_dir = os.path.join(DIR_PATH, 'images')
        inspection_dir = os.path.join(DIR_PATH, 'inspection')
        data_dir = os.path.join(DIR_PATH, 'data')
        out_dir = os.path.join(data_dir, items.form)

        os.makedirs(pdf_dir, exist_ok=True)
        os.makedirs(image_dir, exist_ok=True)
        os.makedirs(inspection_dir, exist_ok=True)
        os.makedirs(out_dir, exist_ok=True)

        pdf_path = Crawler.download(url, items.name, pdf_dir)
        if not pdf_path:
            return
        try:
            im = Oratory1990Crawler.parse_pdf(
                os.path.join(pdf_dir, f'{items.name}.pdf'),
                os.path.join(image_dir, f'{items.name}.png')
            )
        except ValueError as err:
            if str(err) == 'Measured by Crinacle':
                ignored = items.copy()
                ignored.form = 'ignore'
                self.name_index.update(ignored, source_name=items.source_name, name=items.name, form=items.form)
                self.write_name_index()
                print(f'  Ignored {items.source_name} because it is measured by Crinacle.\n')
            return
        fr, inspection = Oratory1990Crawler.parse_image(im, items.name)
        inspection.save(os.path.join(inspection_dir, f'{items.name}.png'))
        fr_path = os.path.join(out_dir, f'{items.name}.csv')
        fr.write_to_csv(fr_path)
        print(f'  Saved CSV to "{fr_path}"')


class RedditCrawlFailed(Exception):
    pass


class GraphParseFailed(Exception):
    pass

