import bfch_techcrunch
import pprint

f = bfch_techcrunch.feedlist()
pprint.pprint(f)
print(len(f))

f = bfch_techcrunch.feed(0)
pprint.pprint(f.to_dict())
print(len(f.to_dict()))

#f = bfch_twitch.search('flor')
#pprint.pprint(f.to_dict())
#print len(f.to_dict())
#
