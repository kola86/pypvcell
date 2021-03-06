"""
   Copyright 2017 Kan-Hua Lee, Toyota Technological Institute

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
"""
from typing import List

from pypvcell.illumination import Illumination
from pypvcell.photocurrent import gen_step_qe, calc_jsc_from_eg, calc_jsc
from .ivsolver import calculate_j01, gen_rec_iv_by_rad_eta, one_diode_v_from_i, \
    solve_mj_iv_obj_with_optimization, one_diode_v_from_i_p, \
    solve_series_connected_ivs, solve_v_from_j_adding_epsilon
from .fom import max_power
from .spectrum import Spectrum, _energy_to_length
from .detail_balanced_MJ import calculate_j01_from_qe
import numpy as np
import scipy.constants as sc
from scipy.optimize import newton, bisect
from scipy.optimize import bisect

thermal_volt = sc.k / sc.e


def rev_diode(voltage):
    rev_j01 = 4.46e-15
    rev_bd_v = 0.1
    return -rev_j01 * np.exp(sc.e * (-voltage - rev_bd_v) / (sc.k * 300) - 1)


def rev_breakdown_diode(voltage):
    rev_j01 = 4.46e-17
    rev_bd_v = 6
    return -rev_j01 * np.exp(sc.e * (-voltage - rev_bd_v) / (sc.k * 300) - 1)


def guess_max_volt(rad_eta, jsc, j01, cell_T):
    """
    Get an estimate of the maximum voltage by given Jsc

    :param rad_eta: radiative efficiency
    :param jsc: Jsc in A/m^2
    :param j01: J01 in A/m^2
    :param cell_T: cell temperature in Kelvin
    :return:
    """

    return rad_eta * np.log(jsc / j01) * thermal_volt * cell_T


class SolarCell(object):

    def __init__(self):

        self.j01 = None
        self.j02 = None
        self.jsc = 0
        self.j01_r = None
        self.j02_r = None

        self.subcell = []

    def get_iv(self):
        raise NotImplementedError()

    def set_input_spectrum(self, input_spectrum):
        """
        Set the illlumination spectrum of the solar cell

        :param input_spectrum: the illumination spectrum.
        :type input_spectrum: Illumination
        """
        raise NotImplementedError()

    def get_transmit_spectrum(self):
        """

        :return: The transmitted spectrum of this solar cell
        :rtype: Illumination
        """

        raise NotImplementedError()

    def get_v_from_j(self, current):

        raise NotImplementedError()

    def get_j_from_v(self, voltage):

        raise NotImplementedError()

    def set_description(self, desp):

        self.desp = desp

    def __str__(self):

        if self.desp == None:
            return "solar cell"
        else:
            return self.desp


def set_input_spectrum(subcells: List[SolarCell], input_spectrum: Illumination):
    filtered_spectrum = None

    # Set spectrum for each subcell
    for i, sc in enumerate(subcells):
        if i == 0:
            sc.set_input_spectrum(input_spectrum)
        else:
            sc.set_input_spectrum(filtered_spectrum)
        filtered_spectrum = sc.get_transmit_spectrum()

    return subcells


class TransparentCell(SolarCell):
    def __init__(self):
        self.ill = None

    def set_input_spectrum(self, input_spectrum):
        self.ill = input_spectrum

    def get_transmit_spectrum(self):
        return self.ill

    def get_eta(self):
        return 0


class SQCell(SolarCell):
    """
    A SolarCell at Shockley-Queisser limit

    """

    def __init__(self, eg: float, cell_T, rad_eta=1, n_c=3.5, n_s=1, approx=False,
                 plug_in_term='default_rev_breakdown'):
        """
        Initialize a solar cell with Shockley-Queisser(SQ) Model
        It loads the class and sets up J01 of the cell

        :param eg: band gap in (eV)
        :param cell_T: cell temperture
        :param rad_eta: radiative efficiency in fraction
        :param n_c: refractive index of cell
        :param n_s: refractive index of ambient
        :param approx: Set False to enable higher order terms for calculating J01
        :param plug_in_term: the term that added into I(V), "default_rev_breakdown": use default reverse breakdown diode
        """

        super().__init__()

        self.eg = eg
        self.cell_T = cell_T
        self.n_c = n_c
        self.n_s = n_s
        self.rad_eta = rad_eta
        self.approx = approx
        self.jsc = 0
        self.j02 = None

        self.desp = 'SQCell'
        self._construct()
        self.subcell = [self]
        if (plug_in_term == "default_rev_breakdown"):
            self.plug_in_term = rev_breakdown_diode
        else:
            self.plug_in_term = plug_in_term

    def _construct(self):

        method = 'ana'
        if method == 'ana':
            self.j01 = calculate_j01(self.eg, temperature=self.cell_T,
                                     n1=1, n_c=self.n_c, n_s=self.n_s, approx=self.approx)
        elif method == 'num':
            qe = gen_step_qe(self.eg, 1)
            self.j01 = calculate_j01_from_qe(qe, n_c=self.n_c, n_s=self.n_s, T=self.cell_T)

        self.j01_r = self.j01 / self.rad_eta

    def set_input_spectrum(self, input_spectrum):
        self.ill = input_spectrum
        self.jsc = calc_jsc_from_eg(input_spectrum, self.eg)

    def get_transmit_spectrum(self):

        sp = self.ill.get_spectrum(to_x_unit='m')

        filter_y = sp[0, :] >= _energy_to_length(self.eg, 'eV', 'm')

        filter = Spectrum(sp[0, :], filter_y, x_unit='m')

        return self.ill * filter

    def get_eta(self):
        volt = np.linspace(-0.5, self.eg, num=300)
        volt, current = gen_rec_iv_by_rad_eta(self.j01, self.rad_eta, 1, self.cell_T, 1e15, voltage=volt, jsc=self.jsc)

        max_p = max_power(volt, current)

        return max_p / self.ill.rsum()

    def get_iv(self, volt=None):
        if volt is None:
            max_volt = guess_max_volt(rad_eta=self.rad_eta, jsc=self.jsc, j01=self.j01, cell_T=self.cell_T) + 0.2
            volt = np.linspace(-10, max_volt, num=1000)

        volt, current = gen_rec_iv_by_rad_eta(self.j01, self.rad_eta, 1, self.cell_T, np.inf, voltage=volt,
                                              jsc=self.jsc, plug_in_term=self.plug_in_term)

        return volt, current

    def get_v_from_j(self, current):

        return one_diode_v_from_i(current, self.j01, rad_eta=self.rad_eta,
                                  n1=1, temperature=self.cell_T, jsc=self.jsc)

    def get_v_from_j_p(self, current):

        return one_diode_v_from_i_p(current, self.j01, rad_eta=self.rad_eta,
                                    n1=1, temperature=self.cell_T, jsc=self.jsc)

    def get_j_from_v(self, volt):

        _, current = gen_rec_iv_by_rad_eta(self.j01, self.rad_eta, 1, self.cell_T, np.inf,
                                           voltage=volt, jsc=self.jsc, plug_in_term=self.plug_in_term)

        return current


class HighPSQCell(SQCell):
    """
    This calculate a SQ-limit solar cell like SQCell,
    but uses the some trick to deal with current at negative voltage numerically

    """

    def get_iv(self, volt=None):
        if volt is None:
            max_volt = guess_max_volt(rad_eta=self.rad_eta, jsc=self.jsc, j01=self.j01, cell_T=self.cell_T) + 0.2
            volt = np.linspace(-10, max_volt, num=1000)

        volt, current = gen_rec_iv_by_rad_eta(self.j01, 1, 1, self.cell_T, np.inf, voltage=volt, jsc=self.jsc)

        return volt, current

    def get_v_from_j(self, current):

        return one_diode_v_from_i(current, self.j01, rad_eta=self.rad_eta, n1=1, temperature=self.cell_T,
                                  jsc=self.jsc, minus_one=False)[0]

    def get_v_from_j_numerical(self, current, x0=0.0):

        def f(x):
            return self.get_j_from_v(x, to_tup=False) - current

        v = newton(f, x0=x0, fprime=self.get_j_from_v_p, fprime2=self.get_j_from_v_pp)

        return v

    def get_v_from_j_p(self, current):

        return one_diode_v_from_i_p(current, self.j01, rad_eta=self.rad_eta,
                                    n1=1, temperature=self.cell_T, jsc=self.jsc)

    def get_j_from_v(self, volt, to_tup=False):

        _, current = gen_rec_iv_by_rad_eta(self.j01, 1, 1, self.cell_T,
                                           np.inf, voltage=volt, jsc=0, minus_one=False)

        tot_current = current - self.jsc

        if to_tup:
            return -self.jsc, current
        else:
            return tot_current

    def get_j_from_v_p(self, volt):

        m = sc.e / sc.k / self.cell_T

        return 1 / self.rad_eta * m * np.exp(m * volt)

    def get_j_from_v_pp(self, volt):

        m = sc.e / sc.k / self.cell_T

        return 1 / self.rad_eta * m * m * np.exp(m * volt)

    def get_single_j_from_v_bisect_fancy(self, voltage, j_tuple):

        def f(x):
            j_dark = np.power(10, x)
            return self.get_v_from_j((j_tuple[0], j_dark)) - voltage

        logj = bisect(f, -20, 1)

        return np.power(10, logj)


class DBCell(SolarCell):
    def __init__(self, qe: Spectrum, rad_eta: float, T: float, n_c=3.5, n_s=1, qe_cutoff=1e-3, eg=None):
        """
        Initialize the solar cell object

        :param eg: band gap (in eV)
        :param T: temperature of the cell
        :param qe: quantum efficiency of the cell
        :type qe: Spectrum
        :param rad_eta: external radiative efficiency of the cell
        :param n_c: refractive index of the cell
        :param n_s: refractive index of ambient
        :param qe_cutoff: set the qe value to zero if it is lower than qe_cutoff. This is for avoiding the small ground in experimental data ends up becoming large when multiplying generalized Planck's distribution.
        """

        super().__init__()

        self.qe = qe
        self.rad_eta = rad_eta
        self.n_c = n_c
        self.n_s = n_s
        self.qe_cutoff = qe_cutoff
        self.cell_T = T

        self.n1 = 1
        self.n2 = 2
        self.eg = eg

        self.ill = None
        self.jsc = None

        self._check_qe()
        self._construct()

        self.subcell = [self]

    def _construct(self):

        self.j01 = calculate_j01_from_qe(self.qe, n_c=self.n_c, n_s=self.n_s, threshold=self.qe_cutoff, T=self.cell_T)

        self.j01_r = self.j01 / self.rad_eta

    def _check_qe(self):
        """
        Check if all the values of QE is <1 and >0
        :return:
        """

        if (np.all(self.qe.core_y >= 0) and np.all(self.qe.core_y <= 1)) == False:
            raise ValueError("The values of QE should be between 0 and 1.")

    def set_input_spectrum(self, input_spectrum):
        self.ill = input_spectrum
        self.jsc = calc_jsc(self.ill, self.qe)

    def get_transmit_spectrum(self):
        """

        :return: the transmitted spetrum
        :rtype: Spectrum
        """

        filtered_sp = self.ill * (1 - self.qe)

        return filtered_sp

    def get_iv(self, volt=None):
        if volt is None:
            volt = np.linspace(-0.5, 5, num=300)

        volt, current = gen_rec_iv_by_rad_eta(self.j01, rad_eta=self.rad_eta, n1=1,
                                              temperature=self.cell_T, rshunt=1e15, voltage=volt, jsc=self.jsc)

        return volt, current

    def get_eta(self):

        # Guess the required limit of maximum voltage
        volt_lim = self.rad_eta * np.log(self.jsc / self.j01) * thermal_volt * self.cell_T

        volt, current = self.get_iv(volt=np.linspace(-0.5, volt_lim + 0.3, 300))

        max_p = max_power(volt, current)

        return max_p / self.ill.rsum()


class MJCell(SolarCell):
    def __init__(self, subcell, connect='2T'):
        """

        :param subcell: A list of SolarCell instances of multijunction cell from top to bottom , e.g. [top_cell mid_cell bot_cell]
        :type subcell: List[SolarCell]
        """

        self.subcell = subcell
        self.connect = connect

    def set_input_spectrum(self, input_spectrum):

        self.ill = input_spectrum
        filtered_spectrum = None

        # Set spectrum for each subcell
        for i, sc in enumerate(self.subcell):
            if i == 0:
                sc.set_input_spectrum(input_spectrum)
            else:
                sc.set_input_spectrum(filtered_spectrum)
            filtered_spectrum = sc.get_transmit_spectrum()

    def get_transmit_spectrum(self):

        return self.subcell[-1].get_transmit_spectrum()

    def get_iv(self, volt=None, verbose=0):

        subcell_voltage = np.linspace(-5, 1.9, num=300)
        all_iv = [(sc.get_iv(volt=subcell_voltage)) for sc in self.subcell]

        # v, i = new_solve_mj_iv(all_iv)
        # v, i = solve_mj_iv_obj_with_optimization(self.subcell, verbose=verbose)

        iv_funcs = [cell.get_j_from_v for cell in self.subcell]

        v, i = solve_series_connected_ivs(iv_funcs, -3, 3, 100)

        return v, i

    def get_j_from_v(self, voltage, verbose=False, max_iter=0):
        # Use iteration based-method to calculate V(J)

        curr_v, curr_i = self.get_iv()

        if (type(voltage) == float) or (type(voltage) == int):
            voltage = np.array([voltage])

        iter_num = 0
        interped_i = np.interp(voltage, curr_v, curr_i)
        while True:

            if iter_num >= max_iter:
                break

            solved_vs = np.empty((len(self.subcell), len(voltage) * 2))
            for subcell_idx, cell in enumerate(self.subcell):
                if iter_num == 0:
                    solved_iv = solve_v_from_j_adding_epsilon(cell.get_j_from_v, interped_i, bisect, epsilon=0.1)
                else:
                    solved_iv = solve_v_from_j_adding_epsilon(cell.get_j_from_v, interped_i, bisect, epsilon=0)
                solved_v = solved_iv[:, 0]
                solved_i = solved_iv[:, 1]
                solved_vs[subcell_idx, :] = solved_v

            solved_v_sum = np.sum(solved_vs, axis=0)

            # add solved result into new array
            interped_i = np.interp(solved_v_sum, curr_v, curr_i)

            curr_v = np.concatenate((curr_v, solved_v_sum))
            curr_i = np.concatenate((curr_i, interped_i))

            sorted_index = np.argsort(curr_v)
            curr_v = curr_v[sorted_index]
            curr_i = curr_i[sorted_index]

            iter_num += 1

        interped_i = np.interp(voltage, curr_v, curr_i)

        return interped_i

    def get_eta(self, verbose=0):

        if self.connect == '2T':

            v, i = self.get_iv(verbose=verbose)

            eta = max_power(v, i) / self.ill.rsum()

        elif self.connect == 'MS':
            mp = 0
            for sc in self.subcell:
                mp += max_power(*sc.get_iv())

            eta = mp / self.ill.rsum()

        return eta

    def get_subcell_jsc(self):

        jsc = np.array([sc.get_iv(0)[1] for sc in self.subcell])

        return jsc


class ResistorCell(SolarCell):

    def __init__(self, resistance):
        self.r = resistance

    def get_v_from_j(self, current):
        return current * self.r

    def get_j_from_v(self, voltage):
        return voltage / self.r

    def get_v_from_j_p(self, current):
        return self.r

    def get_j_from_v_p(self, voltage):
        return 1 / self.r

    def get_j_from_v_by_newton(self, voltage):
        voltage = float(voltage)

        def f(x):
            return self.get_v_from_j(x) - voltage

        j = newton(f, x0=0, fprime=self.get_v_from_j_p)

        return j


class SeriesConnect(SolarCell):
    def __init__(self, s_list):
        self.s_list = s_list

    def get_v_from_j(self, current):

        result_v = np.zeros_like(current)
        for scell in self.s_list:
            v = scell.get_v_from_j(current)
            result_v += v
        return result_v

    def get_v_from_j_p(self, current):

        result_v = np.zeros_like(current)
        for scell in self.s_list:
            v = scell.get_v_from_j_p(current)
            result_v += v
        return result_v

    def get_j_from_v(self, voltage):

        solved_j = []

        for v in voltage:
            j = self.get_single_j_from_v(v)
            solved_j.append(j)

        return np.array(solved_j)

    def get_single_j_from_v(self, voltage, x0=0):

        def f(x):
            return self.get_v_from_j(x) - voltage

        j = newton(f, x0=x0, fprime=self.get_v_from_j_p)

        return j

    def get_single_j_from_v_bisect(self, voltage, a, b):

        from scipy.optimize import bisect

        def f(x):
            return self.get_v_from_j(x) - voltage

        return bisect(f, a, b)


class ParallelConnect(SolarCell):
    def __init__(self, s_list):
        self.s_list = s_list

    def get_j_from_v(self, current):

        result_j = np.zeros_like(current)
        for scell in self.s_list:
            j = scell.get_j_from_v(current)
            result_j += j
        return result_j

    def get_j_from_v_p(self, voltage):

        result_v = np.zeros_like(voltage)
        for scell in self.s_list:
            v = scell.get_j_from_v_p(voltage)
            result_v += v
        return result_v

    def get_v_from_j(self, current):

        solved_j = []

        for v in current:
            j = self.get_single_v_from_j(v)
            solved_j.append(j)

        return np.array(solved_j)

    def get_single_v_from_j(self, current, x0=0.0):

        def f(x):
            return self.get_j_from_v(x) - current

        j = newton(f, x0=x0, fprime=self.get_j_from_v_p)
        # solved_j.append(j)

        return j


class DiodeSeriesConnect(SolarCell):
    def __init__(self, s_list):
        self.s_list = s_list

    def set_input_spectrum(self, input_spectrum):

        self.s_list = set_input_spectrum(self.s_list, input_spectrum)

    def get_v_from_j(self, current):

        if isinstance(current, tuple):
            result_v = np.zeros_like(current[1], dtype=np.float)
        else:
            result_v = np.zeros_like(current, dtype=np.float)

        for scell in self.s_list:
            v = scell.get_v_from_j(current)
            # print("cell v:{}".format(v))
            try:
                result_v += v
            except TypeError:
                print("TypeError happens: v={}", format(v))
        return result_v

    def get_v_from_j_p(self, current):

        result_v = np.zeros_like(current)
        for scell in self.s_list:
            v = scell.get_v_from_j_p(current)
            result_v += v
        return result_v

    def get_j_from_v(self, voltage, to_tup=False):

        return self.get_single_j_from_v(voltage, to_tup)

    def get_single_j_from_v(self, voltage, to_tup=False):

        max_reverse_voltage = -5
        if np.min(voltage) < max_reverse_voltage:
            max_reverse_voltage = np.min(voltage)

        x_data, y_data, y_data_tup = self.construct_iv(reverse_voltage=max_reverse_voltage)

        is_larger = np.greater(voltage, np.min(x_data))
        is_smaller = np.less(voltage, np.max(x_data))
        # print("xdata:", x_data)
        # print("max_reverse_voltage", max_reverse_voltage)
        if (not np.all(is_larger)) or (not np.all(is_smaller)):
            print(voltage)
            raise ValueError("the voltage is outside the range of interpolation: min={}, max={}". \
                             format(np.min(x_data), np.max(x_data)))

        j = np.interp(voltage, x_data, y_data)

        # print(y_data_tup[1])
        j_diode_part = np.interp(voltage, x_data, y_data_tup[1])

        if to_tup:
            return y_data_tup[0], j_diode_part
        else:
            return j

    def construct_iv(self, reverse_voltage=-5.0, points_to_generate=200):

        reverse_j = np.empty((len(self.s_list), 2), dtype=np.float)
        for idx, scell in enumerate(self.s_list):
            reverse_j[idx, :] = (scell.get_j_from_v(reverse_voltage, to_tup=True))

        # take a peak at the voltage
        i_range, j_tuple, v_range = self.construct_v_from_j(points_to_generate, reverse_j)

        min_voltage = np.min(v_range)

        # the minimum voltage yielded by the previous calculation may be slightly larger than
        # reverse_voltage, we therefore needs to run the calculation again to take into account the offset
        # This is not optimized yet.

        volt_diff = min_voltage - reverse_voltage

        if volt_diff > 0:
            for idx, scell in enumerate(self.s_list):
                reverse_j[idx, :] = (scell.get_j_from_v(reverse_voltage - volt_diff - 0.5, to_tup=True))

            i_range, j_tuple, v_range = self.construct_v_from_j(points_to_generate, reverse_j)

        return v_range, j_tuple[0] + np.power(10, i_range), (j_tuple[0], np.power(10, i_range))

    def construct_v_from_j(self, points_to_generate, reverse_j):
        reverse_j = np.array(reverse_j)
        j_tuple = (np.max(reverse_j[:, 0]), reverse_j[np.argmax(reverse_j[:, 0]), 1])
        jsc_index = np.log10(abs(j_tuple[0]))
        start_index = np.log10(j_tuple[1])

        i_range_1 = np.linspace(start_index, jsc_index - 3, int(points_to_generate / 2))
        i_range_2 = np.linspace(jsc_index - 3, jsc_index + 1, int(points_to_generate / 2))

        i_range = np.concatenate((i_range_1, i_range_2))

        v_range = np.zeros_like(i_range, dtype=np.float)

        # iterate by the index of diode term
        for idx, i in enumerate(i_range):
            v = 0
            for scell in self.s_list:
                # print((j_tuple[0], np.power(i,10)))
                v += scell.get_v_from_j((j_tuple[0], np.power(10, i)))
            v_range[idx] = v

        return i_range, j_tuple, v_range
