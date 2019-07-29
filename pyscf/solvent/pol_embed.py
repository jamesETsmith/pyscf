import numpy

from pyscf import lib
from pyscf.lib import logger

import cppe


def pe_scf(mf, pe_state):
    oldMF = mf.__class__

    class SCFWithPE(oldMF):
        def __init__(self, pe_state):
            if not isinstance(pe_state, PolEmbed):
                raise TypeError("Invalid type for pe_state.")
            self._pol_embed = pe_state
            self._pe_energy = 0.0
            self._lock = False  # hack to avoid solving for the induced moments twice in DFT calculations

        def dump_flags(self):
            oldMF.dump_flags(self)
            self._pol_embed.check_sanity()
            self._pol_embed.dump_flags()
            return self

        def get_veff(self, mol, dm, *args, **kwargs):
            vhf = oldMF.get_veff(self, mol, dm)
            if not self._lock:
                epe, vpe = self._pol_embed.kernel(dm)
                self._pe_energy = epe
                vhf += vpe
                self._lock = True
                return lib.tag_array(vhf, epe=epe, vpe=vpe)
            else:
                return lib.tag_array(vhf)

        def energy_elec(self, dm=None, h1e=None, vhf=None):
            if dm is None:
                dm = self.make_rdm1()
            if getattr(vhf, 'epe', None) is None:
                vhf = self.get_veff(self.mol, dm)
            e_tot, e_coul = oldMF.energy_elec(self, dm, h1e, vhf-vhf.vpe)
            e_tot += vhf.epe
            logger.info(self._pol_embed, '  PE Energy = %.15g', vhf.epe)
            self._lock = False
            return e_tot, e_coul

        def nuc_grad_method(self):
            raise NotImplementedError("Nuclear gradients not implemented for PE.")

    mf1 = SCFWithPE(pe_state)
    mf1.__dict__.update(mf.__dict__)
    return mf1


class PolEmbed(lib.StreamObject):
    def __init__(self, mol, options):
        self.mol = mol
        self.stdout = mol.stdout
        self.verbose = mol.verbose
        self.max_memory = mol.max_memory

        if not isinstance(options, cppe.PeOptions):
            raise TypeError("Invalid type for options.")

        self.options = options
        mol = cppe.Molecule()
        for z, coord in zip(self.mol.atom_charges(), self.mol.atom_coords()):
            mol.append(cppe.Atom(z, *coord))

        def callback(output):
            logger.info(self, output)
        self.cppe_state = cppe.CppeState(self.options, mol, callback)
        self.cppe_state.calculate_static_energies_and_fields()
        self.potentials = self.cppe_state.potentials
        self.V_es = None

##################################################
# don't modify the following attributes, they are not input options
        self._keys = set(self.__dict__.keys())

    def dump_flags(self):
        logger.info(self, '******** %s flags ********', self.__class__)
        # logger.info(self, 'lebedev_order = %s (%d grids per sphere)',
        #             self.lebedev_order, gen_grid.LEBEDEV_ORDER[self.lebedev_order])
        # logger.info(self, 'lmax = %s'         , self.lmax)
        # logger.info(self, 'eta = %s'          , self.eta)
        # logger.info(self, 'eps = %s'          , self.eps)
        # logger.debug2(self, 'radii_table %s', self.radii_table)
        return self

    def kernel(self, dm, elec_only=False):
        '''
        '''
        if dm.ndim == 3:
            # UHF
            dm = dm[0] + dm[1]
        if self.V_es is None:
            V_es = numpy.zeros((self.mol.nao, self.mol.nao),
                               dtype=numpy.float64)
            for p in self.potentials:
                moments = []
                for m in p.multipoles:
                    m.remove_trace()
                    moments.append(m.values)
                V_es += self._compute_multipole_potential_integrals(p.position,
                                                                    m.k,
                                                                    moments)
            self.V_es = V_es

        self.cppe_state.energies["Electrostatic"]["Electronic"] = (
            numpy.einsum('ij,ij->', self.V_es, dm)
        )

        n_sitecoords = 3 * self.cppe_state.get_polarizable_site_number()
        V_ind = numpy.zeros((self.mol.nao, self.mol.nao),
                            dtype=numpy.float64)
        if n_sitecoords:
            # TODO: use list comprehensions
            current_polsite = 0
            elec_fields = numpy.zeros(n_sitecoords, dtype=numpy.float64)
            for p in self.potentials:
                if not p.is_polarizable:
                    continue
                elec_fields_s = self._compute_field(p.position, dm)
                elec_fields[3*current_polsite:3*current_polsite + 3] = elec_fields_s
                current_polsite += 1
            self.cppe_state.update_induced_moments(elec_fields, elec_only)
            induced_moments = numpy.array(self.cppe_state.get_induced_moments())
            current_polsite = 0
            for p in self.potentials:
                if not p.is_polarizable:
                    continue
                site = p.position
                V_ind += self._compute_field_integrals(site=site,
                                                       moment=induced_moments[3*current_polsite:3*current_polsite + 3])
                current_polsite += 1
        e = self.cppe_state.total_energy
        if not elec_only:
            vmat = self.V_es + V_ind
        else:
            vmat = V_ind
            e = self.cppe_state.energies["Polarization"]["Electronic"]
        return e, vmat

    def _compute_multipole_potential_integrals(self, site, order, moments):
        if order > 2:
            raise NotImplementedError("""Multipole potential integrals not
                                      implemented for order > 2.""")
        self.mol.set_rinv_orig(site)
        # TODO: only calculate up to requested order!
        integral0 = self.mol.intor("int1e_rinv")
        integral1 = self.mol.intor("int1e_iprinv") + self.mol.intor("int1e_iprinv").transpose(0, 2, 1)
        integral2 = self.mol.intor("int1e_ipiprinv") + self.mol.intor("int1e_ipiprinv").transpose(0, 2, 1) + 2.0 * self.mol.intor("int1e_iprinvip")

        # k = 2: 0,1,2,4,5,8 = XX, XY, XZ, YY, YZ, ZZ
        # add the lower triangle to the upper triangle, i.e.,
        # XY += YX : 1 + 3
        # XZ += ZX : 2 + 6
        # YZ += ZY : 5 + 7
        # and divide by 2
        integral2[1] += integral2[3]
        integral2[2] += integral2[6]
        integral2[5] += integral2[7]
        integral2[1] *= 0.5
        integral2[2] *= 0.5
        integral2[5] *= 0.5

        op = integral0 * moments[0] * cppe.prefactors(0)
        if order > 0:
            op += numpy.einsum('aij,a->ij', integral1,
                               moments[1] * cppe.prefactors(1))
        if order > 1:
            op += numpy.einsum('aij,a->ij',
                               integral2[[0, 1, 2, 4, 5, 8], :, :],
                               moments[2] * cppe.prefactors(2))

        return op

    def _compute_field_integrals(self, site, moment):
        self.mol.set_rinv_orig(site)
        integral = self.mol.intor("int1e_iprinv") + self.mol.intor("int1e_iprinv").transpose(0, 2, 1)
        op = numpy.einsum('aij,a->ij', integral, -1.0*moment)
        return op

    def _compute_field(self, site, D):
        self.mol.set_rinv_orig(site)
        integral = self.mol.intor("int1e_iprinv") + self.mol.intor("int1e_iprinv").transpose(0, 2, 1)
        return numpy.einsum('ij,aij->a', D, integral)
        
