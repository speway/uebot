import datetime as dt
from io import BytesIO
import json
from pathlib import Path
import unittest
from PIL import Image
from schedule_image import render_image
from api.telegram import make_reply
from bot import TZ, Bot, parse_week
from test_bot import META

class ImageTests(unittest.TestCase):
    def snapshot(self):
        return parse_week(json.loads((Path(__file__).parent/'edupage_130.json').read_text()),META)

    def test_real_week_is_valid_telegram_image(self):
        im=Image.open(BytesIO(render_image(self.snapshot())))
        self.assertGreaterEqual(im.width,1080)  # Grid expands to keep every time slot readable.
        self.assertLess(im.width+im.height,10000)
        self.assertLess(im.height/im.width,20)

    def test_week_webhook_reuses_photo(self):
        now=dt.datetime.now(TZ);monday=now.date()-dt.timedelta(days=now.weekday())
        state={'kv':{'image:'+monday.isoformat():{'file_id':'existing-photo'},'last_success':now.isoformat()}}
        r=make_reply({'update_id':1,'message':{'chat':{'id':-123,'type':'supergroup'},'from':{'id':1},'text':'/week'}},state,-123)
        self.assertEqual(r['method'],'sendPhoto')
        self.assertEqual(r['photo'],'existing-photo')

    def test_image_edit_and_no_duplicate(self):
        class API:
            def __init__(self): self.calls=[]
            def photo(self,chat,image,caption,message_id=None):
                self.calls.append(message_id)
                return {'message_id':7,'photo':[{'file_id':'photo'}]}
        snapshot=self.snapshot();api=API();b=Bot(api,-123,':memory:')
        try:
            b.publish_image(snapshot);b.publish_image(snapshot)
            snapshot['lessons'][0]['rooms']=['215'];b.publish_image(snapshot)
            self.assertEqual(api.calls,[None,7])
        finally: b.db.close()

    def test_unknown_photo_delivery_is_not_repeated(self):
        class API:
            def __init__(self): self.count=0
            def photo(self,*args):
                self.count+=1
                raise TimeoutError()
        api=API();b=Bot(api,-123,':memory:')
        try:
            with self.assertRaises(TimeoutError): b.publish_image(self.snapshot())
            b.publish_image(self.snapshot())
            self.assertEqual(api.count,1)
            self.assertTrue(b.get('delivery_attention'))
        finally: b.db.close()
