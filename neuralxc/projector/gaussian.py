from abc import ABC, abstractmethod
import numpy as np
from scipy.special import sph_harm
import scipy.linalg
from sympy import N
from functools import reduce
import time
import math
from ..timer import timer
import neuralxc.config as config
from ..utils import geom
import pyscf.gto.basis as gtobasis
import pyscf.gto as gto
import torch
from torch.nn import Module as TorchModule
from .projector import EuclideanProjector
from .polynomial import RadialProjector
import neuralxc

GAMMA = torch.from_numpy(np.array([1/2,3/4,15/8,105/16,945/32,10395/64,135135/128])*np.sqrt(np.pi))

def parse_basis(basis_instructions):
    full_basis = {}
    basis_strings = {}
    for species in basis_instructions:
        if len(species) < 3:
            basis_strings[species] = open(basis_instructions[species]['basis'],'r').read()
            bas = gtobasis.parse(basis_strings[species])
            mol = gto.M(atom='O 0 0 0', basis = {'O':bas})
            sigma = basis_instructions[species].get('sigma',2.0)
            basis = {}
            for bi in range(mol.atom_nshells(0)):
                l = mol.bas_angular(bi)
                if l not in basis:
                    basis[l] = {'alpha':[],'r_o':[],'coeff':[]}
                # alpha = np.array(b[1:])[:,0]
                alpha = mol.bas_exp(bi)
                coeff = mol.bas_ctr_coeff(bi)
                r_o = alpha**(-1/2)*sigma*(1+l/5)
                basis[l]['alpha'].append(alpha)
                basis[l]['r_o'].append(r_o)
                basis[l]['coeff'].append(coeff)
            basis = [{'l': l,'alpha': basis[l]['alpha'],'r_o': basis[l]['r_o'],'coeff':basis[l]['coeff']} for l in basis]
            full_basis[species] = basis
    return full_basis, basis_strings

class GaussianProjector(EuclideanProjector):

    _registry_name = 'gaussian'
    _unit_test = True

    def __init__(self, unitcell, grid, basis_instructions, **kwargs):
        """Implements GTO basis on euclidean grid

        Parameters
        ------------------
        unitcell, numpy.ndarray float (3,3)
        	Unitcell in bohr
        grid, numpy.ndarray float (3)
        	Grid points per unitcell
        basis_instructions, dict
        	Instructions that define basis
        """
        TorchModule.__init__(self)
        full_basis, basis_strings = parse_basis(basis_instructions)
        basis = {key:val for key,val in basis_instructions.items()}
        basis.update(full_basis)
        self.basis_strings = basis_strings
        EuclideanProjector.__init__(self, unitcell, grid, basis, **kwargs)


    def forward_basis(self, positions, unitcell, grid, my_box):
        """Creates basis set (for projection) for a single atom, on grid points

        Parameters
        ----------
        positions, Tensor (1, 3) or (3)
        	atomic position
        unitcell, Tensor (3,3)
        	Unitcell in bohr
        grid, Tensor (3)
        	Grid points per unitcell
        my_box, Tensor (3: euclid. directions, 2: upper and lower limits)
            Limiting box local gridpoints. Relevant if global grid is decomposed
            with MPI or similar.

        Returns
        --------
        rad, ang, mesh
            Stacked radial and angular functions as well as meshgrid stacked
            with grid in spherical coordinates
        """
        r_o_max = np.max([np.max(b['r_o']) for b in self.basis[self.species]])

        self.set_cell_parameters(unitcell, grid)
        basis = self.basis[self.species]
        box, mesh = self.box_around(positions, r_o_max, my_box)
        box['mesh'] = mesh
        rad, ang  =  self.get_basis_on_mesh(box, basis)
        return rad, ang, torch.cat([mesh.double(), box['radial']])

    def forward_fast(self, rho, positions, unitcell, grid, radials, angulars, my_box):
        """Creates basis set (for projection) for a single atom, on grid points

        Parameters
        ----------
        rho, Tensor (npoints) or (xpoints, ypoints, zpoints)
            electron density on grid
        positions, Tensor (1, 3) or (3)
        	atomic position
        unitcell, Tensor (3,3)
        	Unitcell in bohr
        grid, Tensor (3)
        	Grid points per unitcell
        radials, Tensor ()
            Radial functions on grid, stacked
        angulars, Tensor ()
            Angular functions on grid, stacked
        my_box, Tensor (6, npoints)
            (:3,:) 3d meshgrid
            (3:,:) 3d spherical grid

        Returns
        --------
        rad, ang, mesh
            Stacked radial and angular functions as well as meshgrid
        """
        self.set_cell_parameters(unitcell, grid)
        basis = self.basis[self.species]
        box = {}
        box['mesh'] = my_box[:3]
        box['radial'] = my_box[3:]
        Xm, Ym, Zm = box['mesh'].long()
        return  self.project_onto(rho[Xm,Ym,Zm], radials, angulars, basis, self.basis_strings[self.species], box)

    def get_basis_on_mesh(self, box, basis_instructions):

        angs = []
        rads = []

        box['radial'] = torch.stack(box['radial'])
        for ib, basis in enumerate(basis_instructions):
            l = basis['l']
            r_o_max = np.max(basis['r_o'])
            # filt = (box['radial'][0] <= r_o_max)
            filt = (box['radial'][0] <= 1000000)
            box_rad = box['radial'][:,filt]
            # box_m = box['mesh'][:,filt]
            box_m = box['mesh'][:,filt]
            ang = torch.zeros([2*l+1,filt.size()[0]], dtype=torch.double)
            rad = torch.zeros([len(basis['r_o']),filt.size()[0]], dtype=torch.double)
            # ang[:,filt] = torch.stack(self.angulars_real(l, box_rad[1], box_rad[2])) # shape (m, x, y, z)
            # rad[:,filt] = torch.stack(self.radials(box_rad[0], [basis])[0]) # shape (n, x, y, z)
            # rads.append(rad)
            # angs.append(ang)
            angs.append(torch.stack(self.angulars_real(l, box_rad[1], box_rad[2]))) # shape (m, x, y, z)
            rads.append(torch.stack(self.radials(box_rad[0], [basis])[0])) # shape (n, x, y, z)

        return torch.cat(rads), torch.cat(angs)

    def project_onto(self, rho, rads, angs, basis_instructions, basis_string, box):

        rad_cnt = 0
        ang_cnt = 0
        coeff = []
        for basis in basis_instructions:
            # print(basis)
            l = basis['l']
            len_rad = len(basis['r_o'])
            rad = rads[rad_cnt:rad_cnt+len_rad]
            ang = angs[ang_cnt:ang_cnt + (2*l+1)]
            rad_cnt += len_rad
            ang_cnt += 2*l + 1
            r_o_max = np.max(basis['r_o'])
            filt = (box['radial'][0] <= r_o_max)
            # filt = (box['radial'][0] <= 1000000)
            rad *= self.V_cell
            # coeff.append(torch.einsum('i,mi,ni -> nm', rho[filt], ang[:,filt], rad[:,filt]).reshape(-1))
            coeff.append(torch.einsum('i,mi,ni -> nm', rho, ang, rad).reshape(-1))

        mol = gto.M(atom='O 0 0 0',
                    basis={'O': gtobasis.parse(basis_string)})
        bp = neuralxc.pyscf.BasisPadder(mol)

        coeff = torch.cat(coeff)

        sym = 'O'
        print(bp.indexing_l[sym][0])
        print(bp.indexing_r[sym][0])
        indexing_r = torch.from_numpy(np.array(bp.indexing_r[sym][0])).long()
        indexing_l = torch.from_numpy(np.array(bp.indexing_l[sym][0])).bool()
        coeff = coeff[indexing_r]

        coeff_out = torch.zeros([bp.max_n[sym] * (bp.max_l[sym] + 1)**2], dtype= torch.double)

        coeff_out[indexing_l] = coeff
        return coeff_out


    @classmethod
    def g(cls, r, r_o, alpha, l):
        fc = 1-(.5*(1-torch.cos(np.pi*r/r_o[0])))**8
        N = (2*alpha[0])**(l/2+3/4)*np.sqrt(2)/np.sqrt(GAMMA[l])
        f = r**l*torch.exp(-alpha[0]*r**2)*fc*N
        f[r>r_o[0]] = 0
        return f

    @classmethod
    def get_W(cls, basis):
        return np.eye(3)

    @classmethod
    def radials(cls, r, basis, W = None):
        result = []
        if isinstance(basis, list):
            for b in basis:
                res = []
                for ib, alpha in enumerate(b['alpha']):
                    res.append(cls.g(r, b['r_o'][ib], b['alpha'][ib], b['l']))
                result.append(res)
        elif isinstance(basis, dict):
                result.append([cls.g(r, basis['r_o'], basis['alpha'], basis['l'])])
        return result


class RadialGaussianProjector(GaussianProjector, RadialProjector):

    _registry_name = 'gaussian_radial'
    _unit_test = False

    def __init__(self, grid_coords, grid_weights, basis_instructions, **kwargs):
        """Implements GTO basis on radial grid

        Parameters
        ------------------
        grid_coords, numpy.ndarray (npoints, 3)
        	Coordinates of radial grid points
        grid_weights, numpy.ndarray (npoints)
        	Grid weights for integration
        basis_instructions, dict
        	Instructions that defines basis
        """
        self.grid_coords = torch.from_numpy(grid_coords)
        self.grid_weights = torch.from_numpy(grid_weights)
        self.V_cell = self.grid_weights
        full_basis, basis_strings = parse_basis(basis_instructions)
        basis = {key:val for key,val in basis_instructions.items()}
        basis.update(full_basis)
        self.basis_strings = basis_strings
        self.basis = basis
        self.all_angs = {}
        self.unitcell = self.grid_coords
        self.grid = self.grid_weights

    def forward_fast(self, rho, positions, grid_coords, grid_weights, radials, angulars, my_box):
        self.set_cell_parameters(grid_coords, grid_weights)
        basis = self.basis[self.species]
        box = {}
        box['mesh'] = my_box[0]
        box['radial'] = my_box[1:]
        Xm = box['mesh'].long()
        return  self.project_onto(rho[Xm], radials, angulars, basis, self.basis_strings[self.species], box)
