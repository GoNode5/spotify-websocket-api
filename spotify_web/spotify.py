#!/usr/bin/python

from gevent import monkey; monkey.patch_all()
from ws4py.client.geventclient import WebSocketClient
import base64, binascii, json, pprint, re, requests, string, sys, time, gevent, operator

from .proto import mercury_pb2, metadata_pb2
from .proto import playlist4changes_pb2, playlist4content_pb2
from .proto import playlist4issues_pb2, playlist4meta_pb2
from .proto import playlist4ops_pb2, toplist_pb2

base62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

class Logging():
	log_level = 1

	@staticmethod
	def debug(str):
		if Logging.log_level >= 3:
			print "[DEBUG] " + str

	@staticmethod
	def notice(str):
		if Logging.log_level >= 2:
			print "[NOTICE] " + str

	@staticmethod
	def warn(str):
		if Logging.log_level >= 1:
			print "[WARN] " + str

	@staticmethod
	def error(str):
		if Logging.log_level >= 0:
			print "[ERROR] " + str

class WrapAsync():
	timeout = 5

	def __init__(self, callback, func, *args):
		self.marker = gevent.event.AsyncResult()

		if callback == None:
			callback = self.callback
		elif type(callback) == list:
			callback = callback+[self.callback]
		else:
			callback = [callback, self.callback]

		func(*args, callback=callback)

	def callback(self, *args):
		self.marker.set(args)

	def get_data(self):
		try:
			data = self.marker.get(timeout = self.timeout)

			if len(data) > 0 and type(data[0] == SpotifyAPI):
				data = data[1:]

			return data if len(data) > 1 else data[0]
		except:
			return False

class SpotifyClient(WebSocketClient):
	def set_api(self, api):
		self.api_object = api

	def opened(self):
		self.api_object.login()

class SpotifyUtil():
	@staticmethod
	def gid2id(gid):
		return binascii.hexlify(gid).rjust(32, "0")

	@staticmethod
	def id2uri(uritype, v):
		res = []
		v = int(v, 16)
		while v > 0:
		    res = [v % 62] + res
		    v = v / 62
		id = ''.join([base62[i] for i in res])
		return ("spotify:"+uritype+":"+id).rjust(22, "0")

	@staticmethod
	def uri2id(uri):
		parts = uri.split(":")
		if len(parts) > 3 and parts[3] == "playlist":
			s = parts[4]
		else:
			s = parts[2]

		v = 0
		for c in s:
		    v = v * 62 + base62.index(c)
		return hex(v)[2:-1].rjust(32, "0")

	@staticmethod
	def gid2uri(uritype, gid):
		id = SpotifyUtil.gid2id(gid)
		uri = SpotifyUtil.id2uri(uritype, id)
		return uri

	@staticmethod
	def get_uri_type(uri):
		uri_parts = uri.split(":")

		if len(uri_parts) >= 3 and uri_parts[1] == "local":
			return "local"
		elif len(uri_parts) >=5:
			return uri_parts[3]
		elif len(uri_parts) >=4 and uri_parts[3] == "starred":
			return "playlist"
		elif len(uri_parts) >=3:
			return uri_parts[1]
		else:
			return False

	@staticmethod
	def is_local(uri):
		return SpotifyUtil.get_uri_type(uri) == "local"

class SpotifyAPI():
	def __init__(self, login_callback_func = False):
		self.auth_server = "play.spotify.com"

		self.logged_in_marker = gevent.event.AsyncResult()
		self.username = None
		self.password = None
		self.account_type = None
		self.country = None

		self.settings = None

		self.disconnecting = False
		self.ws = None
		self.seq = 0
		self.cmd_callbacks = {}
		self.login_callback = login_callback_func

	def auth(self, username, password):
		if self.settings != None:
			Logging.warn("You must only authenticate once per API object")
			return False

		headers = {
			"User-Agent": "spotify-websocket-api (Chrome/13.37 compatible-ish)",
		}

		session = requests.session()

		secret_payload = {
			"album": "http://open.spotify.com/album/2mCuMNdJkoyiXFhsQCLLqw",
			"song": "http://open.spotify.com/track/6JEK0CvvjDjjMUBFoXShNZ",
		}

		resp = session.get("https://"+self.auth_server+"/redirect/facebook/notification.php", params=secret_payload, headers = headers)
		data = resp.text

		rx = re.compile("<form><input id=\"secret\" type=\"hidden\" value=\"(.*)\" /></form>")
		r = rx.search(data)

		if not r or len(r.groups()) < 1:
			Logging.error("There was a problem authenticating, no auth secret found")
			self.do_login_callback(False)
			return False
		secret = r.groups()[0]

		login_payload = {
			"type": "sp",
			"username": username,
			"password": password,
			"secret": secret,
		}
		resp = session.post("https://"+self.auth_server+"/xhr/json/auth.php", data=login_payload, headers = headers)
		resp_json = resp.json()

		if resp_json["status"] != "OK":
			Logging.error("There was a problem authenticating, authentication failed")
			self.do_login_callback(False)
			return False

		self.settings = resp.json()["config"]

	def auth_from_json(self, json):
		self.settings = json

	def populate_userdata_callback(self, sp, resp, callback_data):
		self.username = resp["user"]
		self.country = resp["country"]
		self.account_type = resp["catalogue"]
		if self.login_callback != False:
			self.do_login_callback(True)
		else:
			self.logged_in_marker.set(True)
		self.chain_callback(sp, resp, callback_data)

	def logged_in(self, sp, resp):
		self.user_info_request(self.populate_userdata_callback)

	def login(self):
		Logging.notice("Logging in")
		credentials = self.settings["credentials"][0].split(":", 2)
		credentials[2] = credentials[2].decode("string_escape")
		credentials_enc = json.dumps(credentials, separators=(',',':'))

		self.send_command("connect", credentials, self.logged_in)

	def do_login_callback(self, result):
		if self.login_callback != False:
			gevent.spawn(self.login_callback, self, result)
		else:
			self.logged_in_marker.set(False)

	def track_uri(self, track, callback = False):
		tid = self.recurse_alternatives(track)
		if tid == False:
			return False
		args = ["mp3160", tid]
		return self.wrap_request("sp/track_uri", args, callback)

	def parse_metadata(self, sp, resp, callback_data):
		header = mercury_pb2.MercuryReply()
		header.ParseFromString(base64.decodestring(resp[0]))

		if header.status_message == "vnd.spotify/mercury-mget-reply":
			if len(resp) < 2:
				ret = False

			mget_reply = mercury_pb2.MercuryMultiGetReply()
			mget_reply.ParseFromString(base64.decodestring(resp[1]))
			items = []
			for reply in mget_reply.reply:
				if reply.status_code != 200:
					continue

				item = self.parse_metadata_item(reply.content_type, reply.body)
				items.append(item)
			ret = items
		else:
			ret = self.parse_metadata_item(header.status_message, base64.decodestring(resp[1]))

		self.chain_callback(sp, ret, callback_data)

	def parse_metadata_item(self, content_type, body):
		if content_type == "vnd.spotify/metadata-album":
			obj = metadata_pb2.Album()
		elif content_type == "vnd.spotify/metadata-artist":
			obj = metadata_pb2.Artist()
		elif content_type == "vnd.spotify/metadata-track":
			obj = metadata_pb2.Track()
		else:
			Logging.error("Unrecognised metadata type " + content_type)
			return False

		obj.ParseFromString(body)

		return obj

	def parse_toplist(self, sp, resp, callback_data):
		obj = toplist_pb2.Toplist()
		res = base64.decodestring(resp[1])
		obj.ParseFromString(res)
		self.chain_callback(sp, obj, callback_data)

	def parse_playlist(self, sp, resp, callback_data):
		obj = playlist4changes_pb2.ListDump()
		try:
			res = base64.decodestring(resp[1])
			obj.ParseFromString(res)
		except:
			obj = False

		self.chain_callback(sp, obj, callback_data)

	def chain_callback(self, sp, data, callback_data):
		if len(callback_data) > 1:
			callback_data[0](self, data, callback_data[1:])
		elif len(callback_data) == 1:
			callback_data[0](self, data)

	def is_track_available(self, track):
		allowed_countries = []
		forbidden_countries = []
		available = False

		for restriction in track.restriction:
			allowed_str = restriction.countries_allowed
			allowed_countries += [allowed_str[i:i+2] for i in range(0, len(allowed_str), 2)]

			forbidden_str = restriction.countries_forbidden
			forbidden_countries += [forbidden_str[i:i+2] for i in range(0, len(forbidden_str), 2)]

			allowed = not restriction.HasField("countries_allowed") or self.country in allowed_countries
			forbidden = self.country in forbidden_countries and len(forbidden_countries) > 0

			# guessing at names here, corrections welcome
			account_type_map = {
				"premium": 1,
				"unlimited": 1,
				"free": 0
			}

			applicable = account_type_map[self.account_type] in restriction.catalogue

			# enable this to help debug restriction issues
			if False:
				print restriction
				print allowed_countries
				print forbidden_countries
				print "allowed: "+str(allowed)
				print "forbidden: "+str(forbidden)
				print "applicable: "+str(applicable)

			available = allowed == True and forbidden == False and applicable == True
			if available:
				break

		if available:
			Logging.notice(SpotifyUtil.gid2uri("track", track.gid) + " is available!")
		else:
			Logging.notice(SpotifyUtil.gid2uri("track", track.gid) + " is NOT available!")

		return available

	def recurse_alternatives(self, track, attempted = []):
		if self.is_track_available(track):
			return SpotifyUtil.gid2id(track.gid)
		else:
			for alternative in track.alternative:
				if self.is_track_available(alternative):
					return SpotifyUtil.gid2id(alternative.gid)

			for alternative in track.alternative:
				uri = SpotifyUtil.gid2uri("track", alternative.gid)
				if uri not in attempted:
					attempted += [uri]
					subtrack = self.metadata_request(uri)
					return self.recurse_alternatives(subtrack, attempted)
			return False

	def generate_multiget_args(self, metadata_type, requests):
		args = [0]

		if len(requests.request) == 1:
			req = base64.encodestring(requests.request[0].SerializeToString())
			args.append(req)
		else:
			header = mercury_pb2.MercuryRequest()
			header.body = "GET"
			header.uri = "hm://metadata/"+metadata_type+"s"
			header.content_type = "vnd.spotify/mercury-mget-request"

			header_str = base64.encodestring(header.SerializeToString())
			req = base64.encodestring(requests.SerializeToString())
			args.extend([header_str, req])

		return args

	def wrap_request(self, command, args, callback, int_callback = None):
		if callback == False:
			data = WrapAsync(int_callback, self.send_command, command, args).get_data()
			return data
		else:
			callback = [callback] if type(callback) != list else callback
			if int_callback != None:
				int_callback = [int_callback] if type(int_callback) != list else int_callback
				callback = int_callback + callback
			self.send_command(command, args, callback)

	def metadata_request(self, uris, callback = False):
		mercury_requests = mercury_pb2.MercuryMultiGetRequest()

		if type(uris) != list:
			uris = [uris]

		for uri in uris:
			uri_type = SpotifyUtil.get_uri_type(uri)
			if uri_type == "local":
				Logging.warn("Track with URI "+uri+" is a local track, we can't request metadata, skipping")
				continue

			id = SpotifyUtil.uri2id(uri)

			mercury_request = mercury_pb2.MercuryRequest()
			mercury_request.body = "GET"
			mercury_request.uri = "hm://metadata/"+uri_type+"/"+id

			mercury_requests.request.extend([mercury_request])

		args = self.generate_multiget_args(SpotifyUtil.get_uri_type(uris[0]), mercury_requests)

		return self.wrap_request("sp/hm_b64", args, callback, self.parse_metadata)

	def toplist_request(self, toplist_content_type = "track", toplist_type = "user", username = None, region = "global", callback = False):
		if username == None:
			username = self.username

		mercury_request = mercury_pb2.MercuryRequest()
		mercury_request.body = "GET"
		if toplist_type == "user":
			mercury_request.uri = "hm://toplist/toplist/user/"+username
		elif toplist_type == "region":
			mercury_request.uri = "hm://toplist/toplist/region"
			if region != None and region != "global":
				mercury_request.uri += "/"+region
		else:
			return False
		mercury_request.uri += "?type="+toplist_content_type

		# playlists don't appear to work?
		if toplist_type == "user" and toplist_content_type == "playlist":
			if username != self.username:
				return False
			mercury_request.uri = "hm://socialgraph/suggestions/topplaylists"

		req = base64.encodestring(mercury_request.SerializeToString())

		args = [0, req]

		return self.wrap_request("sp/hm_b64", args, callback, self.parse_toplist)

	def playlists_request(self, user, fromnum = 0, num = 100, callback = False):
		if num > 100:
			Logging.error("You may only request up to 100 playlists at once")
			return False

		mercury_request = mercury_pb2.MercuryRequest()
		mercury_request.body = "GET"
		mercury_request.uri = "hm://playlist/user/"+user+"/rootlist?from=" + str(fromnum) + "&length=" + str(num)
		req = base64.encodestring(mercury_request.SerializeToString())

		args = [0, req]

		return self.wrap_request("sp/hm_b64", args, callback, self.parse_playlist)


	def playlist_request(self, uri, fromnum = 0, num = 100, callback = False):
		mercury_requests = mercury_pb2.MercuryRequest()

		playlist = uri.replace("spotify:", "").replace(":", "/")
		mercury_request = mercury_pb2.MercuryRequest()
		mercury_request.body = "GET"
		mercury_request.uri = "hm://playlist/" + playlist + "?from=" + str(fromnum) + "&length=" + str(num)

		req = base64.encodestring(mercury_request.SerializeToString())
		args = [0, req]

		return self.wrap_request("sp/hm_b64", args, callback, self.parse_playlist)

	def playlist_op_track(self, playlist_uri, track_uri, op, callback = None):
		playlist = playlist_uri.split(":")
		user = playlist[2]
		if playlist[3] == "starred":
			playlist_id = "starred"
		else:
			playlist_id = "playlist/"+playlist[4]

		mercury_request = mercury_pb2.MercuryRequest()
		mercury_request.body = op
		mercury_request.uri = "hm://playlist/user/"+user+"/" + playlist_id + "?syncpublished=1"
		req = base64.encodestring(mercury_request.SerializeToString())
		args = [0, req, base64.encodestring(track_uri)]
		self.send_command("sp/hm_b64", args, callback)

	def playlist_add_track(self, playlist_uri, track_uri, callback = None):
		self.playlist_op_track(playlist_uri, track_uri, "ADD", callback)

	def playlist_remove_track(self, playlist_uri, track_uri, callback = None):
		self.playlist_op_track(playlist_uri, track_uri, "REMOVE", callback)

	def set_starred(self, track_uri, starred = True, callback = None):
		if starred:
			self.playlist_add_track("spotify:user:"+self.username+":starred", track_uri, callback)
		else:
			self.playlist_remove_track("spotify:user:"+self.username+":starred", track_uri, callback)

	def search_request(self, query, query_type = "all", max_results = 50, offset = 0, callback = False):
		if max_results > 50:
			Logging.warn("Maximum of 50 results per request, capping at 50")
			max_results = 50

		search_types = {
			"tracks": 1,
			"albums": 2,
			"artists": 4,
			"playlists": 8

		}

		query_type= [k for k, v in search_types.items()] if query_type == "all" else query_type
		query_type = [query_type] if type(query_type) != list else query_type
		query_type = reduce(operator.or_, [search_types[type_name] for type_name in query_type if type_name in search_types])

		args = [query, query_type, max_results, offset]

		return self.wrap_request("sp/search", args, callback)

	def user_info_request(self, callback = None):
		return self.wrap_request("sp/user_info", [], callback)

	def heartbeat(self):
		self.send_command("sp/echo", "h", callback = False)

	def send_command(self, name, args = [], callback = None):
		msg = {
			"name": name,
			"id": str(self.seq),
			"args": args
		}

		if callback != None:
			self.cmd_callbacks[self.seq] = callback
		self.seq += 1

		self.send_string(msg)

	def send_string(self, msg):
		msg_enc = json.dumps(msg, separators=(',',':'))
		Logging.debug("sent " + msg_enc)
		self.ws.send(msg_enc)

	def recv_packet(self, msg):
		Logging.debug("recv " + str(msg))
		packet = json.loads(str(msg))
		if "error" in packet:
			self.handle_error(packet)
			return
		elif "message" in packet:
			self.handle_message(packet["message"])
		elif "id" in packet:
			pid = packet["id"]
			if pid in self.cmd_callbacks:
				callback = self.cmd_callbacks[pid]

				if callback == False:
					Logging.debug("No callback was requested for comamnd "+str(pid)+", ignoring")
				elif type(callback) == list:
					callback[0](self, packet["result"], callback[1:])
				else:
					callback(self, packet["result"])

				self.cmd_callbacks.pop(pid)
			else:
				Logging.debug("Unhandled command response with id " + str(pid))

	def work_callback(self, sp, resp):
		Logging.debug("Got ack for message reply")

	def handle_message(self, msg):
		cmd = msg[0]
		if len(msg) > 1:
			payload = msg[1]
		if cmd == "do_work":
			Logging.debug("Got do_work message, payload: "+payload)
			self.send_command("sp/work_done", ["v1"], self.work_callback)

	def handle_error(self, err):
		if len(err) < 2:
			Logging.error("Unknown error "+str(err))

		major = err["error"][0]
		minor = err["error"][1]

		major_err = {
			12: "Track error",
			13: "Hermes error",
			14: "Hermes service error",
		}

		minor_err = {
			1: "failed to send to backend",
			8: "rate limited",
			408: "timeout",
			429: "too many requests",
		}

		if major in major_err:
			major_str = major_err[major]
		else:
			major_str = "unknown (" + str(major) + ")"

		if minor in minor_err:
			minor_str = minor_err[minor]
		else:
			minor_str = "unknown (" + str(minor) + ")"

		if minor == 0:
			Logging.error(major_str)
		else:
			Logging.error(major_str + " - " + minor_str)

	def event_handler(self):
		while self.disconnecting == False:
			m = self.ws.receive()
			if m is not None:
				self.recv_packet(str(m))
			else:
				break

	def heartbeat_handler(self):
		while self.disconnecting == False:
			gevent.sleep(15)
			self.heartbeat()

	def connect(self, username, password, timeout = 10):
		if self.settings == None:
			 if self.auth(username, password) == False:
			 	return False
			 self.username = username
			 self.password = password

		Logging.notice("Connecting to "+self.settings["aps"]["ws"][0])
		
		try:
			self.ws = SpotifyClient(self.settings["aps"]["ws"][0])
			self.ws.set_api(self)
			self.ws.connect()
			self.greenlets = [
				gevent.spawn(self.event_handler),
				gevent.spawn(self.heartbeat_handler)
			]
			if self.login_callback != False:
				gevent.joinall(self.greenlets)
			else:
				try:
					return self.logged_in_marker.get(timeout=timeout)
				except:
					return False
		except:
			self.disconnect()
			return False

	def disconnect(self):
		self.disconnecting = True
		gevent.sleep(1)
		gevent.killall(self.greenlets)

