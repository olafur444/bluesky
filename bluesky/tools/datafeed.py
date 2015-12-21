import time
import aero
from network import TcpClient
import adsb_decoder as decoder


class Modesbeast(TcpClient):
    def __init__(self, stack, traf):
        super(Modesbeast, self).__init__()
        self.stack = stack
        self.traf = traf
        self.acpool = {}
        self.buffer = ''
        self.default_ac_mdl = "B738"

    def moveToThread(self, target_thread):
        self.socket.moveToThread(target_thread)
        super(Modesbeast, self).moveToThread(target_thread)

    def parse_data(self, data):
        self.buffer += data

        if len(self.buffer) > 2048:
            # process the buffer until the last divider <esc> 0x1a
            # then, reset the buffer with the remainder

            bfdata = [ord(i) for i in self.buffer]
            n = (len(bfdata) - 1) - bfdata[::-1].index(0x1a)
            data = bfdata[:n-1]
            self.buffer = self.buffer[n:]

            messages = self.read_mode_s(data)

            if not messages:
                return

            for msg, ts in messages:
                self.read_message(msg, ts)
        return

    def read_mode_s(self, data):
        '''
        <esc> "1" : 6 byte MLAT timestamp, 1 byte signal level,
            2 byte Mode-AC
        <esc> "2" : 6 byte MLAT timestamp, 1 byte signal level,
            7 byte Mode-S short frame
        <esc> "3" : 6 byte MLAT timestamp, 1 byte signal level,
            14 byte Mode-S long frame
        <esc> "4" : 6 byte MLAT timestamp, status data, DIP switch
            configuration settings (not on Mode-S Beast classic)
        <esc><esc>: true 0x1a
        <esc> is 0x1a, and "1", "2" and "3" are 0x31, 0x32 and 0x33

        timestamp:
        wiki.modesbeast.com/Radarcape:Firmware_Versions#The_GPS_timestamp
        '''

        # split raw data into chunks
        chunks = []
        separator = 0x1a
        piece = []
        for d in data:
            if d == separator:
                # shortest msgs are 11 chars
                if len(piece) > 10:
                    chunks.append(piece)
                piece = []
            piece.append(d)

        # extract messages
        messages = []
        for cnk in chunks:
            msgtype = cnk[1]

            # Mode-S Short Message, 7 byte
            if msgtype == 0x32:
                msg = ''.join('%02X' % i for i in cnk[9:16])

            # Mode-S Short Message, 14 byte
            elif msgtype == 0x33:
                msg = ''.join('%02X' % i for i in cnk[9:23])

            # Other message tupe
            else:
                continue

            ts = time.time()

            messages.append([msg, ts])
        return messages

    def read_message(self, msg, ts):
        """
        Process ADSB messages
        """

        if len(msg) < 28:
            return

        df = decoder.get_df(msg)

        if df == 17:
            addr = decoder.get_icao_addr(msg)
            tc = decoder.get_tc(msg)

            if tc >= 1 and tc <= 4:
                # aircraft identification
                callsign = decoder.get_callsign(msg)
                self.update_callsign(addr, callsign)
            if tc >= 9 and tc <= 18:
                # airbone postion frame
                alt = decoder.get_alt(msg)
                oe = decoder.get_oe_flag(msg)  # odd or even frame
                cprlat = decoder.get_cprlat(msg)
                cprlon = decoder.get_cprlon(msg)
                self.update_cprpos(addr, oe, ts, alt, cprlat, cprlon)
            elif tc == 19:        # airbone velocity frame
                sh = decoder.get_speed_heading(msg)
                if len(sh) == 2:
                    spd = sh[0]
                    hdg = sh[1]
                    self.update_spd_hdg(addr, spd, hdg)
        return

    def update_cprpos(self, addr, oe, ts, alt, cprlat, cprlon):
        if addr in self.acpool:
            ac = self.acpool[addr]
        else:
            ac = {}

        ac['alt'] = alt
        if oe == '1':       # odd frame cpr position
            ac['cprlat1'] = cprlat
            ac['cprlon1'] = cprlon
            ac['t1'] = ts

        if oe == '0':       # even frame cpr position
            ac['cprlat0'] = cprlat
            ac['cprlon0'] = cprlon
            ac['t0'] = ts

        ac['ts'] = time.time()

        self.acpool[addr] = ac
        return

    def update_spd_hdg(self, addr, spd, hdg):
        if addr in self.acpool:
            ac = self.acpool[addr]
        else:
            ac = {}

        ac['speed'] = spd
        ac['heading'] = hdg
        ac['ts'] = time.time()

        self.acpool[addr] = ac
        return

    def update_callsign(self, addr, callsign):
        if addr not in self.acpool:
            self.acpool[addr] = {}

        self.acpool[addr]['callsign'] = callsign
        return

    def update_all_ac_postition(self):
        keys = ('cprlat0', 'cprlat1', 'cprlon0', 'cprlon1')
        for addr, ac in self.acpool.items():
            # check if all needed keys are in dict
            if set(keys).issubset(ac):
                pos = decoder.cpr2position(
                    ac['cprlat0'], ac['cprlat1'],
                    ac['cprlon0'], ac['cprlon1'],
                    ac['t0'], ac['t1']
                )

                # update positions of all aircrafts in the list
                if pos:
                    self.acpool[addr]['lat'] = pos[0]
                    self.acpool[addr]['lon'] = pos[1]
        return

    def stack_all_commands(self):
        """create and stack command"""
        params = ('lat', 'lon', 'alt', 'speed', 'heading', 'callsign')
        for i, d in self.acpool.items():
            # check if all needed keys are in dict
            if set(params).issubset(d):
                acid = d['callsign']
                # check is aircraft is already beening displayed
                if(self.traf.id2idx(acid) < 0):
                    mdl = self.default_ac_mdl
                    v = aero.tas2cas(d['speed'], d['alt'] * aero.ft)
                    cmdstr = 'CRE %s, %s, %f, %f, %f, %d, %d' % \
                        (acid, mdl, d['lat'], d['lon'],
                            d['heading'], d['alt'], v)
                    self.stack.stack(cmdstr)
                else:
                    cmdstr = 'MOVE %s, %f, %f, %d' % \
                        (acid, d['lat'], d['lon'], d['alt'])
                    self.stack.stack(cmdstr)

                    cmdstr = 'HDG %s, %f' % (acid,  d['heading'])
                    self.stack.stack(cmdstr)

                    v_cas = aero.tas2cas(d['speed'], d['alt'] * aero.ft)
                    cmdstr = 'SPD %s, %f' % (acid,  v_cas)
                    self.stack.stack(cmdstr)
        return

    def remove_outdated_ac(self):
        """House keeping, remove old entries (offline > 100s)"""
        for addr, ac in self.acpool.items():
            if 'ts' in ac:
                # threshold, remove ac after 90 seconds of no-seen
                if (int(time.time()) - ac['ts']) > 100:
                    del self.acpool[addr]
                    # remove from sim traffic
                    if 'callsign' in ac:
                        self.stack.stack('DEL %s' % ac['callsign'])
        return

    def debug(self):
        count = 0
        for addr, ac in self.acpool.iteritems():
            print addr,
            count += 1
        print ""
        print "total count: %d" % count
        return

    def update(self):
        if self.is_connected:
            # self.debug()
            self.remove_outdated_ac()
            self.update_all_ac_postition()
            self.stack_all_commands()