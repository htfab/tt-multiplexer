#!/usr/bin/env python3

#
# Main Layout engine for TinyTapout mux system
#
# Copyright (c) 2023 Sylvain Munaut <tnt@246tNt.com>
# SPDX-License-Identifier: Apache-2.0
#

import os
import yaml

from collections import namedtuple

try:
	import svgwrite as svg
except ModuleNotFoundError:
	pass


# ----------------------------------------------------------------------------
# Utility classes
# ----------------------------------------------------------------------------

class ConfigNode:

	def __init__(self, cfg = None):
		self._cfg = cfg or {}

	def __getattr__(self, k):
		v = self._cfg[k]
		if isinstance(v, dict):
			self._cfg[k] = v = ConfigNode(v)
		return v

	def __setattr__(self, k, v):
		if k[0] != '_':
			self._cfg[k] = v
		else:
			super().__setattr__(k,v)

	def __getitem__(self, k):
		return self.__getattr__(k)

	def __setitem__(self, k, v):
		self.__setattr__(k, v)

	@classmethod
	def from_yaml(kls, stream):
		return kls(yaml.load(stream, yaml.FullLoader))


class Point(namedtuple('Point', 'x y')):

	__slots__ = []

	def __add__(self, other):
		return Point(self.x + other.x, self.y + other.y)


class Rect(namedtuple('Rect', 'x0 y0 x1 y1')):

	__slots__ = []

	@property
	def ll(self):
		return Point(self.x0, self.y0)

	@property
	def lr(self):
		return Point(self.x1, self.y0)

	@property
	def ul(self):
		return Point(self.x0, self.y1)

	@property
	def ur(self):
		return Point(self.x1, self.y1)

	@property
	def w(self):
		return self.x1 - self.x0

	@property
	def h(self):
		return self.y1 - self.y0

	@property
	def c(self):
		return Point(
			(self.x0 + self.x1) // 2,
			(self.y0 + self.y1) // 2,
		)

	def move(self, xofs, yofs):
		return Rect(self.x0 + xofs, self.y0 + yofs, self.x1 + xofs, self.y1 + yofs)


class SDim(namedtuple('SDim', 'sites units')):

	@classmethod
	def xdim(kls, cfg, sites):
		return kls(
			sites,
			sites * cfg.pdk.site.width,
		)

	@classmethod
	def ydim(kls, cfg, sites):
		return kls(
			sites,
			sites * cfg.pdk.site.height,
		)


# ----------------------------------------------------------------------------
# Module Placer (Logical Grid)
# ----------------------------------------------------------------------------

class ModuleSlot:

	def __init__(self, cfg_data):
		self.name   = cfg_data['name']
		self.pos_x  = cfg_data.get('x')
		self.pos_y  = cfg_data.get('y')
		self.width  = cfg_data.get('width', 1)
		self.height = cfg_data.get('height', 1)

	def as_dict(self):
		return {
			'name':   self.name,
			'x':      self.pos_x,
			'y':      self.pos_y,
			'width':  self.width,
			'height': self.height,
		}


class ModulePlacer:

	def __init__(self, cfg, mod_file, verbose=False):
		# Save config
		self.cfg = cfg
		self.verbose = verbose

		# Run placement
		self.gen_grid()
		self.load_modules(mod_file)
		self.place_modules()

	def gen_grid(self):
		self.grid = {}
		self.grid_free = set()
		for y in range(self.cfg.tt.grid.y):
			for x in range(self.cfg.tt.grid.x // 2):
				self.grid_free.add( (x, y) )
				self.grid_free.add( (x+16, y) )

	def load_modules(self, mod_file):
		# Load raw data from YAML
		with open(mod_file, 'r') as fh:
			data = yaml.load(fh, Loader=yaml.FullLoader)

		# Create and pre-check each module
		self.modules = []
		for mc in data['modules']:
			# Save data
			mod = ModuleSlot(mc)
			self.modules.append(mod)

			# Size & Position limits
			if mod.height not in [1,2]:
				raise RuntimeError(f"Module '{mod.name:s}' has invalid heigth {mod.height:d}")

			if mod.width not in [1,2,4,8]:
				raise RuntimeError(f"Module '{mod.name:s}' has invalid width {mod.width:d}")

			if (mod.pos_x is not None) and (
					(mod.pos_x < 0) or
					(mod.pos_x >= 32) or
					((mod.pos_x % 16) > self.cfg.tt.grid.x // 2)
				):
				raise RuntimeError(f"Module '{mod.name:s}' has invalid X position {mod.pos_x:d}")

			if (mod.pos_y is not None) and (
					(mod.pos_y < 0) or
					(mod.pos_y >= self.cfg.tt.grid.y)
				):
				raise RuntimeError(f"Module '{mod.name:s}' has invalid Y position {mod.pos_y:d}")

	def save_modules(self, mod_file):
		data = {
			'modules': [ m.as_dict() for m in self.modules ],
		}
		with open(mod_file, 'w') as fh:
			fh.write(yaml.dump(data))

	def _site_suitable(self, mod, pos_x, pos_y):
		# Check all positions are free
		for oy in range(mod.height):
			for ox in range(mod.width):
				if (pos_x+ox, pos_y+oy) not in self.grid_free:
					return False

		# For multi-height, check we're on an odd row
		if mod.height > 1:
			if pos_y & 1 == 0:
				return False

		# For multi-width, check we don't cross mid boundary
		if mod.width > 1:
			if (pos_x ^ (pos_x + mod.width - 1)) & 16:
				return False

		# All checks out
		return True

	def _find_xy_for_module(self, mod):
		# Scan the whole grid in order and check if suitable
		for y in range(self.cfg.tt.grid.y):
			for x in range(self.cfg.tt.grid.x // 2):
				if self._site_suitable(mod, x, y):
					return x, y
			for x in range(self.cfg.tt.grid.x // 2):
				if self._site_suitable(mod, x + 16, y):
					return x + 16, y
		return None, None

	def _find_y_for_module(self, mod):
		for y in range(self.cfg.tt.grid.y):
			if self._site_suitable(mod, mod.pos_x, y):
				return mod.pos_x, y
		return None, None

	def _find_x_for_module(self, mod):
		for x in range(self.cfg.tt.grid.x // 2):
			if self._site_suitable(mod, x, mod.pos_y):
				return x, y
		for x in range(self.cfg.tt.grid.x // 2):
			if self._site_suitable(mod, x + 16, mod.pos_y):
				return x + 16, y
		return None, None

	def _place_module(self, mod):
		# Find final X, Y
		if (mod.pos_x is None) and (mod.pos_y is None):
			x, y = self._find_xy_for_module(mod)
		elif (mod.pos_x is None):
			x, y = self._find_x_for_module(mod)
		elif (mod.pos_y is None):
			x, y = self._find_y_for_module(mod)
		elif self._site_suitable(mod, mod.pos_x, mod.pos_y):
			x, y = mod.pos_x, mod.pos_y
		else:
			x = y = None

		# Valid ?
		if (x is None) or (y is None):
			raise RuntimeError(f"Module '{mod.name:s}' couldn't be placed")

		# Actually place it
		mod.pos_x = x
		mod.pos_y = y

		# And in the grid
		self.grid[ (x, y) ] = mod

		# And remove the sites from the free list
		for oy in range(mod.height):
			for ox in range(mod.width):
				self.grid_free.remove( (x+ox, y+oy) )

		# Debug
		if self.verbose:
			print(f"Module [{mod.width:d}x{mod.height:d}] placed at ({mod.pos_x:2d}, {mod.pos_y:2d}): '{mod.name:s}'")

	def _place_modules_group(self, mods):
		# Sort the modules by size
		mods = sorted(mods, key=lambda m: (m.height, m.width), reverse=True)

		# Attempt to place them one by one
		for m in mods:
			self._place_module(m)

	def place_modules(self):
		# Sort modules into 3 sets
		full_placed = []
		semi_placed = []
		auto_placed = []

		for m in self.modules:
			if   (m.pos_x is not None) and (m.pos_y is not None):
				full_placed.append(m)
			elif (m.pos_x is not None) or  (m.pos_y is not None):
				semi_placed.append(m)
			else:
				auto_placed.append(m)

		# Place them from most constrained to less constrained
		self._place_modules_group(full_placed)
		self._place_modules_group(semi_placed)
		self._place_modules_group(auto_placed)


# ----------------------------------------------------------------------------
# Layout Engine
# ----------------------------------------------------------------------------

class Layout:

	def __init__(self, cfg):
		# Save config
		self.cfg = cfg

		# Sub-layouts
		self.global_layout()
		self.userif_layout()
		self.hspine_layout()
		self.vspine_layout()
		self.ctrl_layout()

	def global_layout(self):
		# Checks
		if self.cfg.tt.grid.x % 4:
			raise RuntimeError("Grid X must be divisible by 4")

		if self.cfg.tt.grid.y % 2:
			raise RuntimeError("Grid Y must be even")

		# Main object
		self.glb = glb = ConfigNode()

		glb.margin = ConfigNode()
		glb.top    = ConfigNode()
		glb.branch = ConfigNode()
		glb.block  = ConfigNode()
		glb.mux    = ConfigNode()
		glb.ctrl   = ConfigNode()

		# Size of the various busses
		self.vspine = ConfigNode({
			'ow': self.cfg.tt.uio.o + self.cfg.tt.uio.io * 2 + 2,
			'iw': self.cfg.tt.uio.i + self.cfg.tt.uio.io     + 10 + 1 + 2,
		})
		self.user = ConfigNode({
			'ow': self.cfg.tt.uio.o + self.cfg.tt.uio.io * 2,
			'iw': self.cfg.tt.uio.i + self.cfg.tt.uio.io,
		})

		# Margins
		glb.margin.x = SDim.xdim(self.cfg, self.cfg.tt.margin.x)
		glb.margin.y = SDim.ydim(self.cfg, self.cfg.tt.margin.y)

		# Horizontal layout
			# Total available space in 'sites'
		h_sites = self.cfg.pdk.die.width // self.cfg.pdk.site.width

			# Reserve space for Spine tracks and IO pad tracks
			# (we keep those tracks twice as far as they strictly need
			#  to be to decrease mutual capacitance)
		vti = self.cfg.pdk.tracks[self.cfg.tt.spine.vlayer]

		vspine_tracks = self.vspine.iw + self.vspine.ow
		iopads_tracks = self.cfg.tt.uio.o + self.cfg.tt.uio.i + 3 * self.cfg.tt.uio.io + 2

		rsvd_width = 2 * vti.x.pitch * (vspine_tracks + iopads_tracks)
		rsvd_sites = (rsvd_width + self.cfg.pdk.site.width - 1) // self.cfg.pdk.site.width
		rsvd_sites = (rsvd_sites + 1) & ~1

			# Remaining size is for the blocks and margin
		tmp_sites = (h_sites - rsvd_sites) // self.cfg.tt.grid.x

			# Final X dimensions
		glb.block.width  = SDim.xdim(self.cfg, tmp_sites - glb.margin.x.sites)
		glb.block.pitch  = SDim.xdim(self.cfg, tmp_sites)
		glb.mux.width    = SDim.xdim(self.cfg, tmp_sites * (self.cfg.tt.grid.x // 2) - glb.margin.x.sites)
		glb.branch.width = glb.mux.width
		glb.ctrl.width   = SDim.xdim(self.cfg,  rsvd_sites)
		glb.top.width    = SDim.xdim(self.cfg,  rsvd_sites + 2 * (glb.mux.width.sites + glb.margin.x.sites))

		# Vertical layout
			# Total available space in 'sites'
		v_sites = self.cfg.pdk.die.height // self.cfg.pdk.site.height

			# Divide up assuming blocks are twice as high
			# as the row-mux
		HM_MUX = 1
		HM_BLK = 2

		tmp_sites = (v_sites // (self.cfg.tt.grid.y // 2)) - (3 * glb.margin.y.sites)
		tmp_sites = tmp_sites // (2 * HM_BLK + HM_MUX)

			# Final dimensions
		glb.block.height  = SDim.ydim(self.cfg, tmp_sites * HM_BLK)
		glb.mux.height    = SDim.ydim(self.cfg, tmp_sites * HM_MUX)
		glb.branch.pitch  = SDim.ydim(self.cfg, (
								(glb.block.height.sites * 2) +
								(glb.mux.height.sites    ) +
								(glb.margin.y.sites     * 3)
							))
		glb.branch.height = SDim.ydim(self.cfg, glb.branch.pitch.sites - glb.margin.y.sites)
		glb.ctrl.height = SDim.ydim(self.cfg, (
								(glb.block.height.sites * 2) +
								(glb.margin.y.sites)
							))
		glb.top.height    = SDim.ydim(self.cfg, glb.branch.pitch.sites * (self.cfg.tt.grid.y // 2) - glb.margin.y.sites)

			# Check mux is high enough for horizontal spine
		hspine_tracks = self.user.iw + self.user.ow + 6 + 1 + 3

		hti = self.cfg.pdk.tracks[self.cfg.tt.spine.hlayer]
		if glb.mux.height.units < (hspine_tracks * hti.y.pitch):
			raise RuntimeError("Mux too small for Horizontal Spine")

	def _ply_len(self, ply):
		return sum([x[1] or 1 for x in block_ply])

	def _ply_expand(self, ply):
		rv = []
		for n, c in ply:
			# Single signal ?
			if c is None:
				# Handle 'skips'
				if n is None:
					rv.append(None)

				# Normal signals
				else:
					rv.append(n)

			# [n-1:0] range ?
			elif type(c) is int:
				# Handle 'skips'
				if n is None:
					for i in range(c):
						rv.append(None)

				# Normal signals
				else:
					for i in range(c-1, -1, -1):
						rv.append(n + '[' + str(i) + ']')

			# [o+n-1:o] offset range ?
			elif type(c) is tuple:
				# Handle 'skips'
				if n is None:
					for i in range(c[1]):
						rv.append(None)

				# Normal signals
				else:
					for i in range(c[0]+c[1]-1, c[0]-1, -1):
						rv.append(n + '[' + str(i) + ']')

			# ???
			else:
				raise RuntimeError('Invalid Pin Layout')

		return rv

	def _ply_distribute(self, n_pins, start, end, step=0, layer='met4', axis='x'):
		# Get tracks data
		t_offset = self.cfg.pdk.tracks[layer][axis].offset
		t_pitch  = self.cfg.pdk.tracks[layer][axis].pitch

		# Generate tracks in the interval
		p = (((start - t_offset) // t_pitch) * t_pitch) + t_offset
		tracks = []
		while p < end:
			tracks.append(p)
			p += t_pitch

		# Pick step if need be
		if step <= 0:
			step = (len(tracks) - 1) // (n_pins - 1)

		if step == 0:
			raise RuntimeError('Too many tracks to fit in too small area ...')

		# Pick the center tracks
		tracks_needed = (n_pins - 1) * step + 1
		i_start = (len(tracks) - tracks_needed) // 2
		i_end   = i_start + tracks_needed

		return tracks[i_start:i_end:step]

	def _ply_finalize(self, pins, tracks):
		if len(pins) != len(tracks):
			raise RuntimeError('Mismatch pin/track list')

		return dict([(p,t) for p,t in zip(pins, tracks) if p is not None])

	def userif_layout(self):

		# Pin Layouts
		block_ply = [
			(None,      None),	# k_zero not mapped
			('uio_oe',  self.cfg.tt.uio.io),
			('uio_out', self.cfg.tt.uio.io),
			('uo_out',  self.cfg.tt.uio.o),
			('uio_in',  self.cfg.tt.uio.io),
			('ui_in',   self.cfg.tt.uio.i - 2),
			('rst_n',   None),
			('clk',     None),
			('ena',     None),
		]

		mux_ply = lambda n: [
			('um_k_zero', (n, 1)),
			('um_ow',     (n * self.user.ow, self.user.ow)),
			('um_iw',     (n * self.user.iw, self.user.iw)),
			('um_ena',    (n, 1)),
		]

		# Expand and check consistency
		block_ply_e = self._ply_expand(block_ply)
		mux_ply_e   = self._ply_expand(mux_ply(0))

		if len(block_ply_e) != len(mux_ply_e):
			raise RuntimeError('Mux and Block pin layout mismatch !')

		# Get tracks
		tracks = self._ply_distribute(
			n_pins = len(block_ply_e),
			start  = 0,
			end    = self.glb.block.width.units,
			step   = 0,
			layer  = self.cfg.tt.spine.vlayer,
			axis   = 'x',
		)

		# Create pins for user blocks
		self.ply_block = self._ply_finalize(block_ply_e, tracks)

		# Create pins for mux blocks
		mux_tracks  = []
		mux_ply_bot = []
		mux_ply_top = []

		for i in range(self.cfg.tt.grid.x // 2):
			ofs = self.glb.block.pitch.units * i
			mux_tracks.extend([x+ofs for x in tracks])
			mux_ply_bot.extend(self._ply_expand(mux_ply(i*2+0)))
			mux_ply_top.extend(self._ply_expand(mux_ply(i*2+1)))

		self.ply_mux_bot = self._ply_finalize(mux_ply_bot, mux_tracks)
		self.ply_mux_top = self._ply_finalize(mux_ply_top, mux_tracks)

	def hspine_layout(self):

		# Pin Layout
			# We try to match the layout of the horizontal bus
			# with the layout of the input pins since a lot of them
			# are 1:1 connections

		hspine_ply = [
			('bus_gd',  (3,1)),			# so_gh
			('bus_ow',  self.user.ow),	# so_usr
			('bus_gd',  (1,2)),			# so_gl, si_gh
			('bus_iw',  self.user.iw),	# si_usr
			('bus_gd',  (0,1)),			# si_sel[9]
			(None,      3),				# si_sel[8:6]
			('bus_sel', (0,1)),			# si_sel[5]
			(None,      1),				# si_sel[4]
			('bus_sel', (1,4)),			# si_sel[3:0]
			('bus_ena', None),			# si_ena
			(None,      8),				# si_gl, k_zero, k_one, addr
		]

		vspine_ply = [
			('spine_ow', self.vspine.ow),	# { so_gh, so_usr, so_gl }
			('spine_iw', self.vspine.iw),	# { si_gh, si_usr, si_sel, si_ena, si_gl }
			('k_zero',   None),
			('k_one',    None),
			('addr',     5),
		]

		# Expand and check consistency
		hspine_ply_e = self._ply_expand(hspine_ply)
		vspine_ply_e = self._ply_expand(vspine_ply)

		if len(hspine_ply_e) != len(vspine_ply_e):
			raise RuntimeError('Mux and Spine pin layout mismatch !')

		# Get tracks
		tracks = self._ply_distribute(
			n_pins = len(hspine_ply_e),
			start  = 0,
			end    = self.glb.mux.height.units,
			step   = 0,
			layer  = self.cfg.tt.spine.hlayer,
			axis   = 'y',
		)

		# Create pins for user blocks
		self.ply_mux_bus  = self._ply_finalize(hspine_ply_e, tracks)
		self.ply_mux_port = self._ply_finalize(vspine_ply_e, tracks)

	def vspine_layout(self):

		# Pin Layout
		ply = [
			('spine_ow', self.vspine.ow),
			(None, 3),	# Leave space for PDN
			('spine_iw', self.vspine.iw),
		]

		# Expand and check consistency
		ply_e = self._ply_expand(ply)

		# Get tracks
		tracks = self._ply_distribute(
			n_pins = len(ply_e),
			start  = 0,
			end    = self.glb.ctrl.width.units,
			step   = 2,
			layer  = self.cfg.tt.spine.vlayer,
			axis   = 'x',
		)

		# Create pins for control block
		self.ply_ctrl_vspine = self._ply_finalize(ply_e, tracks)

	def ctrl_layout(self):
		# Config for up/down mapping of IO pads
		# FIXME: Part of this should be from config and is sky130 specific
		LOW  =  8		# 8 pins are lower than control block
		PADS = 38		# 38 pins total and mapped at the end except for 6 control pins
		OFFS = 38 - (self.cfg.tt.uio.o + self.cfg.tt.uio.i + self.cfg.tt.uio.io) - 6

		# List of pads
		usr_pads = (
			[ ( 'i', i) for i in range(self.cfg.tt.uio.i ) ] +
			[ ( 'o', i) for i in range(self.cfg.tt.uio.o ) ] +
			[ ('io', i) for i in range(self.cfg.tt.uio.io) ]
		)

		usr_pad = [ (a,b,i+OFFS) for (i, (a,b)) in enumerate(usr_pads) ]

		# Classify and order them up / down
		bl_pads  = []
		br_pads  = []
		top_pads = []

		for t, ti, gi in usr_pad:
			# Bottom Right
			if gi < LOW:
				br_pads.append( (t, ti) )

			# Bottom Left
			elif gi >= PADS - LOW:
				bl_pads.append( (t, ti) )

			# Top side
			else:
				top_pads.insert(0, (t, ti))

		# Create expanded pins for each
		def expand(l):
			rv = []
			for t, ti in l:
				if t == 'i':
					rv.append(f'pad_ui_in[{ti:d}]')

				elif t == 'o':
					rv.append(f'pad_uo_out[{ti:d}]')

				elif t == 'io':
					rv.append(f'pad_uio_in[{ti:d}]')
					rv.append(f'pad_uio_out[{ti:d}]')
					rv.append(f'pad_uio_oe_n[{ti:d}]')
			return rv

		bl_pads  = expand(bl_pads)
		br_pads  = expand(br_pads)
		top_pads = expand(top_pads)

		# Append the control signal on the bottom left side
		bl_pads.extend([
			'k_one',
			'k_zero',
			'ctrl_sel_rst_n',
			'ctrl_sel_inc',
			'ctrl_ena',
		])

		# Split top into left/right
		ts = len(top_pads) // 2

		tl_pads = top_pads[0:ts]
		tr_pads = top_pads[ts:]

		# Assign tracks
		def spread(ply, ll, hl):
			tracks = self._ply_distribute(
				n_pins = len(ply),
				start  = ll,
				end    = hl,
				step   = 2,
				layer  = self.cfg.tt.spine.vlayer,
				axis   = 'x',
			)

			return self._ply_finalize(ply, tracks)

		limit_left  = min(self.ply_ctrl_vspine.values())
		limit_right = max(self.ply_ctrl_vspine.values())

		self.ply_ctrl_io_top = {}
		self.ply_ctrl_io_bot = {}
		self.ply_ctrl_io_top.update( spread(tl_pads, 0,           limit_left) )
		self.ply_ctrl_io_bot.update( spread(bl_pads, 0,           limit_left) )
		self.ply_ctrl_io_top.update( spread(tr_pads, limit_right, self.glb.ctrl.width.units) )
		self.ply_ctrl_io_bot.update( spread(br_pads, limit_right, self.glb.ctrl.width.units) )


# ----------------------------------------------------------------------------
# Layout elements
# ----------------------------------------------------------------------------

LayoutElementPlacement = namedtuple('LayoutElementPlacement', 'elem pos orient name')

MacroInstance = namedtuple('MacroInstance', 'inst_name mod_name pos orient elem')


class LayoutElement:

	mod_name = None

	color = None

	def __init__(self, layout, width, height, parent=None):
		self.layout = layout
		self.width  = width
		self.height = height
		self.parent = parent
		self.children = {}

	def add_child(self, child, pos, orient='N', name=None):
		# Set parent
		child.parent = self

		# Add to children
		self.children[child] = LayoutElementPlacement(child, pos, orient, name)

	def get_sub_macros(self):
		# Scan children and returned named ones
		rv = []

		for cp in self.children.values():
			# Only named ones
			if cp.name is None:
				continue

			# Get sub macro from the child
			sml = cp.elem.get_sub_macros()

			# If we're not a leaf, we need to transform the return list
			# with whatever transform is applied to that child
			for sm in sml:
				if cp.orient == 'N':
					pos    = cp.pos + sm.pos
					orient = sm.orient

				elif cp.orient == 'FN':
					pos = cp.pos + Point(
						cp.elem.width - (sm.pos.x + sm.elem.width),
						sm.pos.y
					)
					orient = {
						'N':  'FN',
						'FS': 'S',
					}[sm.orient]

				else:
					raise RuntimeError('Not implemented')

				rv.append(MacroInstance(
					cp.name + '.' + sm.inst_name,
					sm.mod_name,
					pos,
					orient,
					sm.elem
				))

			# That child is a leaf, so just return it
			else:
				if cp.elem.mod_name is not None:
					rv.append( MacroInstance(cp.name, cp.elem.mod_name, cp.pos, cp.orient, cp.elem) )

		return rv


	def _render(self, dwg):
		# Draw self
		if self.color is not None:
			dwg.add(svg.shapes.Rect(
				( 0, 0 ),
				( self.width, self.height ),
				fill = self.color,
			))

		# Recurse
		for cp in self.children.values():
			# Create group and add to drawing
			cg = svg.container.Group()
			dwg.add(cg)

			# Apply transform placing it in design
			cg.translate(cp.pos[0], cp.pos[1])

			# Apply orientation transform
			if cp.orient == 'N':
				# Nothing to do
				pass

			elif cp.orient == 'FS':
				# Flipped South
				cg.translate(0, cp.elem.height)
				cg.scale(1, -1)

			elif cp.orient == 'FN':
				# Flipped North
				cg.translate(cp.elem.width, 0)
				cg.scale(-1, 1)

			else:
				raise RuntimeError('Unsupported orientation')

			# Render
			cp.elem._render(cg)

	def draw_to_svg(self, fn):
		# Create drawing
		dwg = svg.Drawing(
			size = (
				self.layout.cfg.pdk.die.width  / 1e3,
				self.layout.cfg.pdk.die.height / 1e3,
			),
		)

		# Group for invert-y and scaling
		grp = svg.container.Group()
		grp.scale(1e-3, 1e-3)
		grp.translate(0, self.layout.cfg.pdk.die.height)
		grp.scale(1, -1)
		dwg.add(grp)

		# Draw this
		self._render(grp)

		# Save result
		dwg.write(open(fn, 'w'))


class Block(LayoutElement):

	color = 'crimson'

	def __init__(self, layout, mod_name=None, mw=1, mh=1):
		# Compute module actual width / height
		width = (
			mw * layout.glb.block.width.units +
			(mw - 1) * layout.glb.margin.x.units
		)

		height = (
			mh * layout.glb.block.height.units +
			(mh - 1) * layout.glb.margin.y.units
		)

		# Set module name
		self.mod_name = mod_name

		# Super
		super().__init__(
			layout,
			width,
			height,
		)

	def _render(self, dwg):
		# Super call
		super()._render(dwg)

		# Render pins
		for pn, pp in self.layout.ply_block.items():
			dwg.add(svg.shapes.Rect(
				( pp-150, self.height-1000 ),
				( 300,    1000),
				fill = 'black',
			))


class Mux(LayoutElement):

	mod_name = 'tt_mux'

	color = 'mediumslateblue'

	def __init__(self, layout):
		# Super
		super().__init__(
			layout,
			layout.glb.mux.width.units,
			layout.glb.mux.height.units,
		)

	def _render(self, dwg):
		# Super call
		super()._render(dwg)

		# Render pins
			# User blocks bottom
		for pn, pp in self.layout.ply_mux_bot.items():
			dwg.add(svg.shapes.Rect(
				( pp-150, 0 ),
				( 300,    1000),
				fill = 'black',
			))

			# User blocks top
		for pn, pp in self.layout.ply_mux_top.items():
			dwg.add(svg.shapes.Rect(
				( pp-150, self.height-1000 ),
				( 300,    1000),
				fill = 'black',
			))

			# V-Spine connection
		for pn, pp in self.layout.ply_mux_port.items():
			dwg.add(svg.shapes.Rect(
				( self.width-1000, pp-150 ),
				( 1000, 300),
				fill = 'black',
			))


class Branch(LayoutElement):

	def __init__(self, layout, placer, branch_id):
		# Super
		super().__init__(
			layout,
			layout.glb.branch.width.units,
			layout.glb.branch.height.units,
		)

		# Create Mux
		self.mux = mux = Mux(layout)

		mux_x = 0
		mux_y = layout.glb.block.height.units + layout.glb.margin.y.units

		self.add_child(mux, Point(0, mux_y), 'N', name='mux_I')

		# Create Blocks
		self.blocks_bot = []
		self.blocks_top = []

		for bx in range(layout.cfg.tt.grid.x):
			# Grid position
			grid_y = (branch_id >> 1) * 2  + (bx  & 1)
			grid_x = (branch_id  & 1) * 16 + (bx >> 1)

			# Is there anything here ?
			mp = placer.grid.get( (grid_x, grid_y) )
			if mp is None:
				continue

			# Block instance
			block = Block(layout, mod_name=f'tt_um_{mp.name:s}', mw=mp.width, mh=mp.height)

			# Even is bottom / Odd it top
			if bx & 1:
				self.blocks_top.append(block)
				blk_y = self.height - layout.glb.block.height.units
			else:
				self.blocks_bot.append(block)
				blk_y = 0

			blk_x = (bx >> 1) * layout.glb.block.pitch.units

			# Name
			name = f'col_um\\[{bx>>1:d}\\].um_{"top" if (bx & 1) else "bot":s}_I.block_{grid_y:d}_{grid_x:d}.tt_um_I'

			# Create Block
			self.add_child(block, Point(blk_x, blk_y), 'FS' if (bx & 1) else 'N', name=name)


class Controller(LayoutElement):

	mod_name = 'tt_ctrl'

	color = 'yellowgreen'

	def __init__(self, layout):
		# Super
		width  = layout.glb.ctrl.width.units
		height = layout.glb.ctrl.height.units

		super().__init__(
			layout,
			width,
			height,
		)

	def _render(self, dwg):
		# Super call
		super()._render(dwg)

		# Render pins
			# Vspine connections
		for pn, pp in self.layout.ply_ctrl_vspine.items():
			dwg.add(svg.shapes.Rect(
				( pp-150, 0 ),
				( 300,    self.height),
				fill = 'black',
			))

			# IO top
		for pn, pp in self.layout.ply_ctrl_io_top.items():
			dwg.add(svg.shapes.Rect(
				( pp-150, self.height-1000 ),
				( 300,    1000),
				fill = 'black',
			))

			# IO bottom
		for pn, pp in self.layout.ply_ctrl_io_bot.items():
			dwg.add(svg.shapes.Rect(
				( pp-150, 0 ),
				( 300,    1000),
				fill = 'black',
			))


class Top(LayoutElement):
	"""
	Top level grid aligned area containing all TT elements
	"""

	color = 'lightslategray'

	def __init__(self, layout, placer):
		# Super
		super().__init__(
			layout,
			layout.glb.top.width.units,
			layout.glb.top.height.units,
		)

		# Create Branches
		self.branches = []

		for by in range(0, layout.cfg.tt.grid.y):
			# Even is left / Odd is right
			if by & 1:
				b_x = self.width - layout.glb.branch.width.units
			else:
				b_x = 0

			b_y = (by >> 1) * layout.glb.branch.pitch.units

			branch = Branch(layout, placer, by)
			self.branches.append(branch)

			self.add_child(branch, Point(b_x, b_y), 'FN' if (by & 1) else 'N', name=f'branch\\[{by:d}\\]')

		# Create Controller
		self.ctrl = ctrl = Controller(layout)

		ctrl_x = layout.glb.branch.width.units + layout.glb.margin.x.units
		ctrl_y = (layout.cfg.tt.grid.y // 4) * layout.glb.branch.pitch.units - \
			( layout.glb.block.height.units + layout.glb.margin.y.units )

		self.add_child(ctrl, Point(ctrl_x, ctrl_y), 'N', name='ctrl_I')


class Die(LayoutElement):
	"""
	Full Die User Area Element
	"""

	color = 'silver'

	def __init__(self, layout, placer):
		# Super
		super().__init__(
			layout,
			layout.cfg.pdk.die.width,
			layout.cfg.pdk.die.height,
		)

		# Add 'Top'
		self.top = top = Top(layout, placer)

		top_x = (self.width  - top.width)  // 2
		top_y = (self.height - top.height) // 2

		self.add_child(top, Point(top_x, top_y), 'N', name='top_I')


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

class TinyTapeout:

	def __init__(self, config=None, modules=None):
		# Determine the config files
		config  = self.get_config_file(config)
		modules = self.get_modules_file(modules) if (modules is not False) else False

		# Generic config / layout
		self.cfg    = ConfigNode.from_yaml(open(config, 'r'))
		self.layout = Layout(self.cfg)

		# Modules placement
		if modules is not False:
			self.placer = ModulePlacer(self.cfg, modules)
			self.die    = Die(self.layout, self.placer)

	@classmethod
	def _get_data_file(kls, user_val, env_var, default_val):
		base = os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir, 'cfg'))
		val = os.getenv(env_var, user_val) or default_val
		if not os.path.isabs(val):
			val = os.path.join(base, val)
		return val

	@classmethod
	def get_config_file(kls, config=None):
		return kls._get_data_file(config, 'TT_CONFIG', 'sky130.yaml')

	@classmethod
	def get_modules_file(kls, modules=None):
		return kls._get_data_file(modules, 'TT_MODULES', 'modules_placed.yaml')