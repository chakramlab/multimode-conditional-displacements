#V1: Basic ECD for multimode cavities
#V2: ECD with a qutrit instead of qubit 
'''
V2 notes: 
1. make ancilla 3 level
2. each layer consists of ge, ef rotations and a ge ECD gate 

Date: August 11, 2023
'''

#%%
# note: timestamp can't use "/" character for h5 saving.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
END_OPT_STRING = "\n" + "=" * 60 + "\n"
import numpy as np
import tensorflow as tf

tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.ERROR)  # supress warnings
import h5py

print(
    "\nNeed tf version 2.3.0 or later. Using tensorflow version: "
    + tf.__version__
    + "\n"
)
import ECD_control.ECD_optimization.tf_quantum as tfq
from ECD_control.ECD_optimization.visualization import VisualizationMixin
import qutip as qt
import datetime
import time


class BatchOptimizer(VisualizationMixin):

    # a block is defined as the unitary: CD(beta)R_phi(theta)
    #if include_final_displacement is true, the gate set will include a final displacement at the end.
    def __init__(
        self,
        optimization_type="state transfer",
        target_unitary=None,
        P_cav=None,
        N_cav=None,
        initial_states=None,
        target_states=None,
        N_modes =1 ,
        N_single_layer = 1,
        N_ancilla_levels = 2,
        N_multistart=10,
        N_blocks=20,
        term_fid=0.99,  # can set >1 to force run all epochs
        dfid_stop=1e-4,  # can be set= -1 to force run all epochs
        learning_rate=0.01,
        epoch_size=10,
        epochs=100,
        beta_scale=1.0,
        final_disp_scale=1.0,
        theta_scale=np.pi,
        no_CD_end=False,
        include_final_displacement = False,
        beta_mask=None,
        phi_mask=None,
        theta_mask=None,
        final_disp_mask=None,
        BCH_approx = True,
        name="ECD_control",
        filename=None,
        comment="",
        real_part_only=False,  # include the phase in the optimization cost function. Important for unitaries.
        timestamps=[],
        **kwargs
    ):
        '''
        N_single layer : if =1 , only adds ge rotation in a single layer; if 2 , adds in both ge and ef ancilla rotations
        '''
        self.parameters = {
            "optimization_type": optimization_type,
            "N_modes": N_modes,
            "N_multistart": N_multistart,
            "N_ancilla_levels": N_ancilla_levels,
            "N_single_layer": N_single_layer,
            "N_blocks": N_blocks,
            "N_layers":N_blocks, # redundancy
            "term_fid": term_fid,
            "dfid_stop": dfid_stop,
            "no_CD_end": no_CD_end,
            "BCH_approx": BCH_approx,
            "learning_rate": learning_rate,
            "epoch_size": epoch_size,
            "epochs": epochs,
            "beta_scale": beta_scale,
            "final_disp_scale": final_disp_scale,
            "theta_scale": theta_scale,
            "include_final_displacement": include_final_displacement,
            "real_part_only": real_part_only,
            "name": name,
            "comment": comment,
        }
        self.parameters.update(kwargs)
        if (
            self.parameters["optimization_type"] == "state transfer"
            or self.parameters["optimization_type"] == "analysis"
        ):
            self.batch_fidelities = (
                self.batch_state_transfer_fidelities_real_part
                if self.parameters["real_part_only"]
                else self.batch_state_transfer_fidelities
            )
            # set fidelity function

            self.initial_states = tf.stack(
                [tfq.qt2tf(state) for state in initial_states]
            )

            self.target_unitary = tfq.qt2tf(target_unitary)

            # if self.target_unitary is not None: TODO
            #     raise Exception("Need to fix target_unitary multi-state transfer generation!")

            self.target_states = (  # store dag
                tf.stack([tfq.qt2tf(state) for state in target_states])
                if self.target_unitary is None
                else self.target_unitary @ self.initial_states
            )

            self.target_states_dag = tf.linalg.adjoint(
                self.target_states
            )  # store dag to avoid having to take adjoint
            print(N_cav)
            N_cav = N_cav#self.initial_states[0].numpy().shape[0] // 2
        elif self.parameters["optimization_type"] == "unitary":
            self.target_unitary = tfq.qt2tf(target_unitary)
            N_cav = self.target_unitary.numpy().shape[0] // 2
            P_cav = P_cav if P_cav is not None else N_cav
            raise Exception("Need to implement unitary optimization")

        elif self.parameters["optimization_type"] == "expectation":
            raise Exception("Need to implement expectation optimization")
        elif (
            self.parameters["optimization_type"] == "calculation"
        ):  # using functions but not doing opt
            pass
        else:
            raise ValueError(
                "optimization_type must be one of {'state transfer', 'unitary', 'expectation', 'analysis', 'calculation'}"
            )

        self.parameters["N_cav"] = N_cav
        if P_cav is not None:
            self.parameters["P_cav"] = P_cav

        # TODO: handle case when you pass initial params. In that case, don't randomize, but use "set_tf_vars()"
        self.randomize_and_set_vars()

        self._construct_needed_matrices()

        self._construct_optimization_masks(beta_mask, final_disp_mask, phi_mask,theta_mask)

        # opt data will be a dictionary of dictonaries used to store optimization data
        # the dictionary will be addressed by timestamps of optmization.
        # each opt will append to opt_data a dictionary
        # this dictionary will contain optimization parameters and results

        self.timestamps = timestamps
        self.filename = (
            filename
            if (filename is not None and filename != "")
            else self.parameters["name"]
        )
        path = self.filename.split(".")
        if len(path) < 2 or (len(path) == 2 and path[-1] != ".h5"):
            self.filename = path[0] + ".h5"

    def modify_parameters(self, **kwargs):
        # currently, does not support changing optimization type.
        # todo: update for multi-state optimization and unitary optimziation
        parameters = kwargs
        for param, value in self.parameters.items():
            if param not in parameters:
                parameters[param] = value
        # handle things that are not in self.parameters:
        parameters["initial_states"] = (
            parameters["initial_states"]
            if "initial_states" in parameters
            else self.initial_states
        )
        parameters["target_states"] = (
            parameters["target_states"]
            if "target_states" in parameters
            else self.target_states
        )
        parameters["filename"] = (
            parameters["filename"] if "filename" in parameters else self.filename
        )
        parameters["timestamps"] = (
            parameters["timestamps"] if "timestamps" in parameters else self.timestamps
        )
        self.__init__(**parameters)

    def multimode_baby_matrices(self, tensor_mat, mode_idx):
        '''
        Helper function for construct_needed_matrices()
        
        Input: Matrix for specified mode, mode_index is between 0 and total modes-2
        Output: Tensor product above matrix with identity for other modes
        '''
        full_tensor_mat = None
      
            
        operator_mode = tf.linalg.LinearOperatorFullMatrix(tensor_mat.numpy())
        operator_id = tf.linalg.LinearOperatorFullMatrix((self.identity).numpy())
        op_sequence = [operator_id for j in range(mode_idx)] + [operator_mode] + [operator_id for j in range(self.parameters['N_modes'] -1 -mode_idx)]
        tensor_prod = tf.linalg.LinearOperatorKronecker(op_sequence)
        #print('oi')
        #print(mode_idx)
        return tf.cast((tensor_prod.to_dense()).numpy(), dtype = tf.complex64)
       
    

    def _construct_needed_matrices(self):
        '''
        EG: assuming all modes have same dimensions
        '''
        N_cav = self.parameters["N_cav"]
        q = tfq.position(N_cav)
        p = tfq.momentum(N_cav)
        a = tfq.destroy(N_cav)
        adag = tfq.create(N_cav)
        self.identity = tfq.identity(N_cav)
        self.identity_mm = self.multimode_baby_matrices(self.identity, 0)

        self.identity_ancilla_mm_system =tfq.identity(self.parameters['N_ancilla_levels']*
                                                     (N_cav ** self.parameters['N_modes'] ))
        # Pre-diagonalize and listify
        self.a_mm = []
        self.adag_mm = []
        self._eig_q_mm = []
        self._eig_p_mm = []
        self._U_q_mm = []
        self._U_p_mm = []
        self._qp_comm_mm = []

        for mode_idx in range(self.parameters['N_modes']):
            q_m = self.multimode_baby_matrices( q, mode_idx)
            p_m = self.multimode_baby_matrices( p, mode_idx)
            a_m = self.multimode_baby_matrices( a, mode_idx)
            adag_m = self.multimode_baby_matrices( adag, mode_idx)
            
            (eig_q, U_q) = tf.linalg.eigh(q_m)
            (eig_p, U_p) = tf.linalg.eigh(p_m)
            qp_comm = tf.linalg.diag_part(q_m @ p_m - p_m @ q_m)

            self._eig_q_mm.append(eig_q)
            self._eig_p_mm.append(eig_p)
            self._U_q_mm.append(U_q)
            self._U_p_mm.append(U_p)
            self._qp_comm_mm.append(qp_comm)
            self.a_mm.append(a_m)
            self.adag_mm.append(adag_m)

        #listify (for all modes)
        # self.a_mm = [self.multimode_baby_matrices( a, mode_idx) 
        #                 for mode_idx in range(self.parameters['N_modes'])]
        # self.adag_mm = [self.multimode_baby_matrices( adag, mode_idx) 
        #                 for mode_idx in range(self.parameters['N_modes'])]
        # self._U_q_mm = [self.multimode_baby_matrices( self._U_q, mode_idx) 
        #                 for mode_idx in range(self.parameters['N_modes'])]
        # self._U_p_mm = [self.multimode_baby_matrices( self._U_p, mode_idx) 
        #                 for mode_idx in range(self.parameters['N_modes'])]

        # self._qp_comm_mm = [self.multimode_baby_matrices(self._qp_comm, mode_idx) 
        #                 for mode_idx in range(self.parameters['N_modes'])]

        if self.parameters["optimization_type"] == "unitary":
            P_cav = self.parameters["P_cav"]
            partial_I = np.array(qt.identity(N_cav))
            for j in range(P_cav, N_cav):
                partial_I[j, j] = 0
            partial_I = qt.Qobj(partial_I)
            self.P_matrix = tfq.qt2tf(qt.tensor(qt.identity(2), partial_I))

    def _construct_optimization_masks(
        self, beta_mask=None, final_disp_mask=None, phi_mask=None,theta_mask=None
    ):
        if beta_mask is None:
            beta_mask = np.ones(
                shape=(self.parameters['N_modes'], self.parameters["N_blocks"], self.parameters["N_multistart"]),
                dtype=np.float32,
            )
            if self.parameters["no_CD_end"]:
                beta_mask[-1, :] = 0  # don't optimize final CD
        else:
            # TODO: add mask to self.parameters for saving if it's non standard!
            raise Exception(
                "need to implement non-standard masks for batch optimization"
            )
        if final_disp_mask is None:
            final_disp_mask = np.ones(
                shape=(1, self.parameters["N_multistart"]), dtype=np.float32,
            )
        else:
            raise Exception(
                "need to implement non-standard masks for batch optimization"
            )
        if phi_mask is None:
            phi_mask = np.ones(
                shape=(self.parameters['N_modes'], 
                       self.parameters["N_blocks"], 
                       self.parameters["N_single_layer"],
                       self.parameters["N_multistart"]),
                dtype=np.float32,
            )
        else:
            raise Exception(
                "need to implement non-standard masks for batch optimization"
            )
        if theta_mask is None:
            theta_mask = np.ones(
                shape=(self.parameters['N_modes'], 
                       self.parameters["N_blocks"], 
                       self.parameters["N_single_layer"],
                       self.parameters["N_multistart"]),
                dtype=np.float32,
            )
        else:
            raise Exception(
                "need to implement non-standard masks for batch optimization"
            )
        self.beta_mask = beta_mask
        self.final_disp_mask = final_disp_mask
        self.phi_mask = phi_mask
        self.theta_mask = theta_mask

    @tf.function
    def batch_construct_displacement_operators(self, alphas, mode_idx):

        # Reshape amplitudes for broadcast against diagonals
        sqrt2 = tf.math.sqrt(tf.constant(2, dtype=tf.complex64))
        re_a = tf.reshape(
            sqrt2 * tf.cast(tf.math.real(alphas), dtype=tf.complex64),
            [alphas.shape[0], alphas.shape[1], 1],
        )
        im_a = tf.reshape(
            sqrt2 * tf.cast(tf.math.imag(alphas), dtype=tf.complex64),
            [alphas.shape[0], alphas.shape[1], 1],
        )

        # Exponentiate diagonal matrices
        expm_q = tf.linalg.diag(tf.math.exp(1j * im_a * self._eig_q_mm[mode_idx]))
        expm_p = tf.linalg.diag(tf.math.exp(-1j * re_a * self._eig_p_mm[mode_idx]))
        expm_c = tf.linalg.diag(tf.math.exp(-0.5 * re_a * im_a * self._qp_comm_mm[mode_idx]))

        # Apply Baker-Campbell-Hausdorff
        if self.parameters['BCH_approx']:
            D_mode =  tf.cast(
                self._U_q_mm[mode_idx]
                @ expm_q
                @ tf.linalg.adjoint(self._U_q_mm[mode_idx])
                @ self._U_p_mm[mode_idx]
                @ expm_p
                @ tf.linalg.adjoint(self._U_p_mm[mode_idx])
                @ expm_c,
                dtype=tf.complex64,
            )
        else: #exact form (at least exact up to under-the-hood-tensorflow standard)
            alphas_star = tf.math.conj(alphas)
            exponent = tf.einsum('ij,kl->ijkl', alphas, self.adag_mm[mode_idx]) - tf.einsum('ij,kl->ijkl', alphas_star, self.a_mm[mode_idx])
            D_mode = tf.linalg.expm(exponent)
        # print('ho')
        # print(self.ad.shape)

        return D_mode


    @tf.function
    def batch_construct_singlemode_ancilla_rotation(
        self, phis, thetas, version = 'ge'
    ):
        '''
        contructs rotation op for ancilla 
        note type can be 'ge' or 'ef'
        
        Author: EG
        '''

        # First reshape for later multiplication with displacement ops
        # new shape: ( N_layers, N_multistart, 1, 1)
        

        Phis = phis - tf.constant(np.pi, dtype=tf.float32) / tf.constant(
            2, dtype=tf.float32
        )
        Thetas = thetas / tf.constant(2, dtype=tf.float32)
        
        Phis = tf.cast(
            tf.reshape(Phis, [Phis.shape[0], Phis.shape[1], 1, 1]), dtype=tf.complex64
        )
        
        Thetas = tf.cast(
            tf.reshape(Thetas, [Thetas.shape[0], Thetas.shape[1], 1, 1]),
            dtype=tf.complex64,
        )

        exp = tf.math.exp(tf.constant(1j, dtype=tf.complex64) * Phis)
        exp_dag = tf.linalg.adjoint(exp)
        cos = tf.math.cos(Thetas)
        sin = tf.math.sin(Thetas)

        # constructing the blocks of the matrix
        ul = cos * self.identity_mm
        ll = tf.constant(-1j, dtype=tf.complex64)* exp * sin * self.identity_mm
        ur = tf.constant(-1j, dtype=tf.complex64) * exp_dag * sin * self.identity_mm
        lr = cos * self.identity_mm

        zeroes = tf.cast(tf.zeros(ul.shape), dtype = tf.complex64)

        #creating identity 
        # orig shape : shape(identity of mulltimode)
        # new shape: N_layers x N_multistarts x shape(identity of multimode)
        ones =  tf.stack([self.identity_mm] * self.parameters["N_multistart"])
        ones =  tf.stack([ones] * self.parameters["N_layers"])
        # print('shape of zeroes is ')
        # print(tf.shape(zeroes))

        if self.parameters["N_ancilla_levels"] ==2: 
            return tf.concat([tf.concat([ul, ur], 3), tf.concat([ll, lr], 3)], 2) # normal rotation matrix for qubit 
        
        elif self.parameters["N_ancilla_levels"] == 3: # qutrit mode
            
            if version == 'ge':
                return tf.concat([tf.concat([ul, ur, zeroes], 3), 
                                  tf.concat([ll, lr, zeroes], 3),
                                  tf.concat([zeroes, zeroes, ones], 3)], 2) # normal rotation matrix for qubit but with 3 levels
            
            elif version == 'ef':
                return tf.concat([tf.concat([ones, zeroes, zeroes], 3),
                                  tf.concat([zeroes, ul, ur ], 3), 
                                  tf.concat([zeroes, ll, lr], 3),
                                  ], 2) 

    @tf.function    
    def batch_contruct_singlemode_ECD_operators(
            self, betas_rho, betas_angle, version, mode):
        '''
        COnstructs ECD(beta) = D(beta/2)|e><g| + h.c. and equivalent form if 
        type == ef

        Author: EG
        '''
        Bs = (
            tf.cast(betas_rho, dtype=tf.complex64)
            / tf.constant(2, dtype=tf.complex64)
            * tf.math.exp(
                tf.constant(1j, dtype=tf.complex64)
                * tf.cast(betas_angle, dtype=tf.complex64)
            )
        )

        ds_g = self.batch_construct_displacement_operators(Bs, mode)
        ds_e = tf.linalg.adjoint(ds_g)
        
        zeroes = tf.cast(tf.zeros( ds_g.shape), dtype = tf.complex64)
        #creating identity 
        # orig shape : shape(identity of mulltimode)
        # new shape: N_layers x N_multistarts x shape(identity of multimode)
        ones =  tf.stack([self.identity_mm] * self.parameters["N_multistart"])
        ones =  tf.stack([ones] * self.parameters["N_layers"])
        # print('shape of zeroes is ')
        # print(tf.shape(zeroes))

        #contructing ECD block

        if self.parameters["N_ancilla_levels"] == 2: #qubit
            return tf.concat([tf.concat([zeroes, ds_e], 3),
                               tf.concat([ds_g, zeroes], 3)], 2) # normal rotation matrix for qubit 
        
        elif self.parameters["N_ancilla_levels"] == 3: # qutrit mode
            
            if version == 'ge':
                return tf.concat([tf.concat([zeroes, ds_e, zeroes], 3), 
                                  tf.concat([ds_g, zeroes, zeroes], 3),
                                  tf.concat([zeroes, zeroes, ones], 3)], 2) # normal rotation matrix for qubit 
            
            elif version == 'ef':
                return tf.concat([tf.concat([ones, zeroes, zeroes], 3),
                                  tf.concat([zeroes, zeroes, ds_e,], 3), 
                                  tf.concat([zeroes, ds_g, zeroes], 3),
                                  ], 2) # normal rotation matrix for qubit 
        



    @tf.function
    def batch_construct_singlemode_block_operators(
        self, betas_rho, betas_angle,  phis, thetas, mode
    ):
        '''
        Construct a layer for a single mode
        '''

        # original indixes :  N_layers xN_single_layerx N_multistart 
        # new indixes :  N_single_layer  x N_layers x N_multistart
        # swap indices 
        # betas_rho = tf.einsum('ijk -> jik', betas_rho)  # uncomment when adding in ef ECD
        # betas_angle = tf.einsum('ijk -> jik', betas_angle)
        phis = tf.einsum('ijk -> jik', phis)
        thetas = tf.einsum('ijk -> jik', thetas)

        # print('shape of ecd ops')
        # print(tf.shape(self.batch_contruct_singlemode_ECD_operators(betas_rho, betas_angle, version = 'ge', mode = mode)))

        # print('shape of rotation ops')
        # print(tf.shape(self.batch_construct_singlemode_ancilla_rotation(phis[0], thetas[0], version = 'ge')))
        
        mat = tf.cast(
            # self.batch_contruct_singlemode_ECD_operators(betas_rho[1], betas_angle[1], version = 'ef', mode = mode)
            # @ self.batch_construct_singlemode_ancilla_rotation(phis[3], thetas[3], version = 'ef')
            # @ self.batch_construct_singlemode_ancilla_rotation(phis[2], thetas[2], version = 'ge')
            self.batch_contruct_singlemode_ECD_operators(betas_rho, betas_angle, version = 'ge', mode = mode)
            @ self.batch_construct_singlemode_ancilla_rotation(phis[0], thetas[0], version = 'ge')
            @ self.batch_construct_singlemode_ancilla_rotation(phis[1], thetas[1], version = 'ef')
            , dtype = tf.complex64)
            
        return mat
    
    @tf.function
    def batch_construct_multimode_block_operators(
        self, betas_rho, betas_angle, final_disp_rho, final_disp_angle, phis, thetas
    ):
        '''
        Combines single mode block/layers

        Author: EG
        '''
        #compute single mode blocks
        modes_blocks = []
        for mode in range(0,self.parameters["N_modes"]):
            mode_blocks = self.batch_construct_singlemode_block_operators(betas_rho[mode], betas_angle[mode], phis[mode], thetas[mode], mode) 
            #above var contains all the layers for a specific mode
            modes_blocks.append(mode_blocks) 

        #combines single mode blocks
        mm_blocks = modes_blocks[0] 
        # print('ho')
        # print(mode_blocks.shape)
        for mode in range(1, self.parameters["N_modes"]): 
            mm_blocks = tf.einsum(
                "lmij, lmjk -> lmik", 
                mm_blocks, 
                modes_blocks[mode]
            )
        return mm_blocks


    

    # batch computation of <D>
    # todo: handle non-pure states (rho)
    def characteristic_function(self, psi, betas):
        psi = tfq.qt2tf(psi)
        betas_flat = betas.flatten()
        betas_tf = tf.constant(
            [betas_flat]
        )  # need to add extra dimension since it usually batches circuits
        Ds = tf.squeeze(self.batch_construct_displacement_operators(betas_tf))
        num_pts = betas_tf.shape[1]
        psis = tf.constant(np.array([psi] * num_pts))
        C = tf.linalg.adjoint(psis) @ Ds @ psis
        return np.squeeze(C.numpy()).reshape(betas.shape)

    def characteristic_function_rho(self, rho, betas):
        rho = tfq.qt2tf(rho)
        betas_flat = betas.flatten()
        betas_tf = tf.constant(
            [betas_flat]
        )  # need to add extra dimension since it usually batches circuits
        Ds = tf.squeeze(self.batch_construct_displacement_operators(betas_tf))
        num_pts = betas_tf.shape[1]
        rhos = tf.constant(np.array([rho] * num_pts))
        C = tf.linalg.trace(Ds @ rhos)
        return np.squeeze(C.numpy()).reshape(betas.shape)

    @tf.function
    def batch_state_transfer_fidelities(
        self, betas_rho, betas_angle, final_disp_rho, final_disp_angle, phis, thetas
    ):
        # EG: I'm just gonna ignore this final disp angle
        bs = self.batch_construct_multimode_block_operators(
            betas_rho, betas_angle,
            final_disp_rho, final_disp_angle, 
            phis, thetas
        )
        psis = tf.stack([self.initial_states] * self.parameters["N_multistart"])
        for U in bs:
            psis = tf.einsum(
                "mij,msjk->msik", U, psis
            )  # m: multistart, s:multiple states
        overlaps = self.target_states_dag @ psis  # broadcasting
        overlaps = tf.reduce_mean(overlaps, axis=1)
        overlaps = tf.squeeze(overlaps)
        # squeeze after reduce_mean which uses axis=1,
        # which will not exist if squeezed before for single state transfer
        fids = tf.cast(overlaps * tf.math.conj(overlaps), dtype=tf.float32)
        return fids

    # here, including the relative phase in the cost function by taking the real part of the overlap then squaring it.
    # need to think about how this is related to the fidelity.
    # @tf.function
    # def batch_state_transfer_fidelities_real_part(
    #     self, betas_rho, betas_angle, final_disp_rho, final_disp_angle, phis, thetas
    # ):
    #     bs = self.batch_construct_block_operators(
    #         betas_rho, betas_angle, final_disp_rho, final_disp_angle, phis, thetas
    #     )
    #     psis = tf.stack([self.initial_states] * self.parameters["N_multistart"])
    #     for U in bs:
    #         psis = tf.einsum(
    #             "mij,msjk->msik", U, psis
    #         )  # m: multistart, s:multiple states
    #     overlaps = self.target_states_dag @ psis  # broadcasting
    #     overlaps = tf.reduce_mean(tf.math.real(overlaps), axis=1)
    #     overlaps = tf.squeeze(overlaps)
    #     # squeeze after reduce_mean which uses axis=1,
    #     # which will not exist if squeezed before for single state transfer
    #     # don't need to take the conjugate anymore
    #     fids = tf.cast(overlaps * overlaps, dtype=tf.float32)
    #     return fids

    @tf.function
    def mult_bin_tf(self, a):
        while a.shape[0] > 1:
            if a.shape[0] % 2 == 1:
                a = tf.concat(
                    [a[:-2], [tf.matmul(a[-2], a[-1])]], 0
                )  # maybe there's a faster way to deal with immutable constants
            a = tf.matmul(a[::2, ...], a[1::2, ...])
        return a[0]

    @tf.function
    def U_tot(self,):
        bs = self.batch_construct_block_operators(
            self.betas_rho,
            self.betas_angle,
            self.final_disp_rho,
            self.final_disp_angle,
            self.phis,
            self.thetas,
        )
        # U_c = tf.scan(lambda a, b: tf.matmul(b, a), bs)[-1]
        U_c = self.mult_bin_tf(
            tf.reverse(bs, axis=[0])
        )  # [U_1,U_2,..] -> [U_N,U_{N-1},..]-> U_N @ U_{N-1} @ .. @ U_1
        # U_c = self.I
        # for U in bs:
        #     U_c = U @ U_c
        return U_c

    def optimize(self, do_prints=True):

        timestamp = datetime.datetime.now().strftime(TIMESTAMP_FORMAT)
        self.timestamps.append(timestamp)
        print("Start time: " + timestamp)
        # start time
        start_time = time.time()
        optimizer = tf.optimizers.Adam(self.parameters["learning_rate"])
        if self.parameters["include_final_displacement"]:
            variables = [
                self.betas_rho,
                self.betas_angle,
                self.final_disp_rho,
                self.final_disp_angle,
                self.phis,
                self.thetas,
            ]
        else:
            variables = [
                self.betas_rho,
                self.betas_angle,
                self.phis,
                self.thetas,
            ]

        @tf.function
        def entry_stop_gradients(target, mask):
            mask_h = tf.abs(mask - 1)
            return tf.stop_gradient(mask_h * target) + mask * target

        @tf.function
        def loss_fun(fids):
            # I think it's important that the log is taken before the avg
            losses = tf.math.log(1 - fids)
            avg_loss = tf.reduce_sum(losses) / self.parameters["N_multistart"]
            return avg_loss

        def callback_fun(obj, fids, dfids, epoch):
            elapsed_time_s = time.time() - start_time
            time_per_epoch = elapsed_time_s / epoch if epoch != 0 else 0.0
            epochs_left = self.parameters["epochs"] - epoch
            expected_time_remaining = epochs_left * time_per_epoch
            fidelities_np = np.squeeze(np.array(fids))
            betas_np, final_disp_np, phis_np, thetas_np = self.get_numpy_vars()
            if epoch == 0:
                self._save_optimization_data(
                    timestamp,
                    fidelities_np,
                    betas_np,
                    final_disp_np,
                    phis_np,
                    thetas_np,
                    elapsed_time_s,
                    append=False,
                )
            else:
                self._save_optimization_data(
                    timestamp,
                    fidelities_np,
                    betas_np,
                    final_disp_np,
                    phis_np,
                    thetas_np,
                    elapsed_time_s,
                    append=True,
                )
            avg_fid = tf.reduce_sum(fids) / self.parameters["N_multistart"]
            max_fid = tf.reduce_max(fids)
            avg_dfid = tf.reduce_sum(dfids) / self.parameters["N_multistart"]
            max_dfid = tf.reduce_max(dfids)
            extra_string = " (real part)" if self.parameters["real_part_only"] else ""
            if do_prints:
                print(
                    "\r Epoch: %d / %d Max Fid: %.6f Avg Fid: %.6f Max dFid: %.6f Avg dFid: %.6f"
                    % (
                        epoch,
                        self.parameters["epochs"],
                        max_fid,
                        avg_fid,
                        max_dfid,
                        avg_dfid,
                    )
                    + " Elapsed time: "
                    + str(datetime.timedelta(seconds=elapsed_time_s))
                    + " Remaing time: "
                    + str(datetime.timedelta(seconds=expected_time_remaining))
                    + extra_string,
                    end="",
                )

        initial_fids = self.batch_fidelities(
            self.betas_rho,
            self.betas_angle,
            self.final_disp_rho,
            self.final_disp_angle,
            self.phis,
            self.thetas,
        )
        fids = initial_fids
        callback_fun(self, fids, 0, 0)
        try:  # will catch keyboard inturrupt
            for epoch in range(self.parameters["epochs"] + 1)[1:]:
                for _ in range(self.parameters["epoch_size"]):
                    with tf.GradientTape() as tape:
                        betas_rho = entry_stop_gradients(self.betas_rho, self.beta_mask)
                        betas_angle = entry_stop_gradients(
                            self.betas_angle, self.beta_mask
                        )
                        if self.parameters["include_final_displacement"]:
                            final_disp_rho = entry_stop_gradients(
                                self.final_disp_rho, self.final_disp_mask
                            )
                            final_disp_angle = entry_stop_gradients(
                                self.final_disp_angle, self.final_disp_mask
                            )
                        else:
                            final_disp_rho = self.final_disp_rho
                            final_disp_angle = self.final_disp_angle
                        phis = entry_stop_gradients(self.phis, self.phi_mask)
                        thetas = entry_stop_gradients(self.thetas, self.theta_mask)
                        new_fids = self.batch_fidelities(
                            betas_rho,
                            betas_angle,
                            final_disp_rho,
                            final_disp_angle,
                            phis,
                            thetas,
                        )
                        new_loss = loss_fun(new_fids)
                        dloss_dvar = tape.gradient(new_loss, variables)
                    optimizer.apply_gradients(zip(dloss_dvar, variables))
                dfids = new_fids - fids
                fids = new_fids
                callback_fun(self, fids, dfids, epoch)
                condition_fid = tf.greater(fids, self.parameters["term_fid"])
                condition_dfid = tf.greater(dfids, self.parameters["dfid_stop"])
                if tf.reduce_any(condition_fid):
                    print("\n\n Optimization stopped. Term fidelity reached.\n")
                    termination_reason = "term_fid"
                    break
                if not tf.reduce_any(condition_dfid):
                    print("\n max dFid: %6f" % tf.reduce_max(dfids).numpy())
                    print("dFid stop: %6f" % self.parameters["dfid_stop"])
                    print(
                        "\n\n Optimization stopped.  No dfid is greater than dfid_stop\n"
                    )
                    termination_reason = "dfid"
                    break
        except KeyboardInterrupt:
            print("\n max dFid: %6f" % tf.reduce_max(dfids).numpy())
            print("dFid stop: %6f" % self.parameters["dfid_stop"])
            print("\n\n Optimization stopped on keyboard interrupt")
            termination_reason = "keyboard_interrupt"

        if epoch == self.parameters["epochs"]:
            termination_reason = "epochs"
            print(
                "\n\nOptimization stopped.  Reached maximum number of epochs. Terminal fidelity not reached.\n"
            )
        self._save_termination_reason(timestamp, termination_reason)
        timestamp_end = datetime.datetime.now().strftime(TIMESTAMP_FORMAT)
        elapsed_time_s = time.time() - start_time
        epoch_time_s = elapsed_time_s / epoch
        step_time_s = epoch_time_s / self.parameters["epochs"]
        self.print_info()
        print("all data saved as: " + self.filename)
        print("termination reason: " + termination_reason)
        print("optimization timestamp (start time): " + timestamp)
        print("timestamp (end time): " + timestamp_end)
        print("elapsed time: " + str(datetime.timedelta(seconds=elapsed_time_s)))
        print(
            "Time per epoch (epoch size = %d): " % self.parameters["epoch_size"]
            + str(datetime.timedelta(seconds=epoch_time_s))
        )
        print(
            "Time per Adam step (N_multistart = %d, N_cav = %d): "
            % (self.parameters["N_multistart"], self.parameters["N_cav"])
            + str(datetime.timedelta(seconds=step_time_s))
        )
        print(END_OPT_STRING)
        return timestamp

    # if append is True, it will assume the dataset is already created and append only the
    # last aquired values to it.
    # TODO: if needed, could use compression when saving data.
    def _save_optimization_data(
        self,
        timestamp,
        fidelities_np,
        betas_np,
        final_disp_np,
        phis_np,
        thetas_np,
        elapsed_time_s,
        append,
    ):
        if not append:
            with h5py.File(self.filename, "a") as f:
                grp = f.create_group(timestamp)
                for parameter, value in self.parameters.items():
                    grp.attrs[parameter] = value
                grp.attrs["termination_reason"] = "outside termination"
                grp.attrs["elapsed_time_s"] = elapsed_time_s
                if self.target_unitary is not None:
                    grp.create_dataset(
                        "target_unitary", data=self.target_unitary.numpy()
                    )
                grp.create_dataset("initial_states", data=self.initial_states.numpy())
                grp.create_dataset("target_states", data=self.target_states.numpy())
                # dims = [[2, int(self.initial_states[0].numpy().shape[0] / 2)], [1, 1]]
                grp.create_dataset(
                    "fidelities",
                    chunks=True,
                    data=[fidelities_np],
                    maxshape=(None, self.parameters["N_multistart"]),
                )
                '''
                EG: Note the way data is stored is different than 
                '''
                grp.create_dataset(
                    "betas",
                    data=[betas_np],
                    chunks=True,
                    maxshape=(
                        None,
                        self.parameters["N_multistart"],
                        self.parameters["N_modes"],
                        self.parameters["N_blocks"], 
                   #     self.parameters["N_single_layer"]
                    ),
                )
                grp.create_dataset(
                    "final_disp",
                    data=[final_disp_np],
                    chunks=True,
                    maxshape=(None, self.parameters["N_multistart"], 1,),
                )
                grp.create_dataset(
                    "phis",
                    data=[phis_np],
                    chunks=True,
                    maxshape=(
                        None,
                        self.parameters["N_multistart"],
                        self.parameters["N_modes"],
                        self.parameters["N_blocks"], 
                        self.parameters["N_single_layer"]
                    ),
                )
                grp.create_dataset(
                    "thetas",
                    data=[thetas_np],
                    chunks=True,
                    maxshape=(
                        None,
                        self.parameters["N_multistart"],
                        self.parameters["N_modes"],
                        self.parameters["N_blocks"], 
                        self.parameters["N_single_layer"]
                    ),
                )
        else:  # just append the data
            with h5py.File(self.filename, "a") as f:
                f[timestamp]["fidelities"].resize(
                    f[timestamp]["fidelities"].shape[0] + 1, axis=0
                )
                f[timestamp]["betas"].resize(f[timestamp]["betas"].shape[0] + 1, axis=0)
                f[timestamp]["final_disp"].resize(
                    f[timestamp]["final_disp"].shape[0] + 1, axis=0
                )
                f[timestamp]["phis"].resize(f[timestamp]["phis"].shape[0] + 1, axis=0)
                f[timestamp]["thetas"].resize(
                    f[timestamp]["thetas"].shape[0] + 1, axis=0
                )

                f[timestamp]["fidelities"][-1] = fidelities_np
                f[timestamp]["betas"][-1] = betas_np
                f[timestamp]["final_disp"][-1] = final_disp_np
                f[timestamp]["phis"][-1] = phis_np
                f[timestamp]["thetas"][-1] = thetas_np
                f[timestamp].attrs["elapsed_time_s"] = elapsed_time_s

    def _save_termination_reason(self, timestamp, termination_reason):
        with h5py.File(self.filename, "a") as f:
            f[timestamp].attrs["termination_reason"] = termination_reason

    def randomize_and_set_vars(self):
        beta_scale = self.parameters["beta_scale"]
        final_disp_scale = self.parameters["final_disp_scale"]
        theta_scale = self.parameters["theta_scale"]
        betas_rho = np.random.uniform(
            0,
            beta_scale,
            size=(  self.parameters["N_modes"],
                    self.parameters["N_blocks"], 
     #               self.parameters["N_single_layer"],
                    self.parameters["N_multistart"],),
        )
        betas_angle = np.random.uniform(
            -np.pi,
            np.pi,
            size=(  self.parameters["N_modes"],
                    self.parameters["N_blocks"], 
     #               self.parameters["N_single_layer"],
                    self.parameters["N_multistart"],),
        )
        if self.parameters["include_final_displacement"]:
            final_disp_rho = np.random.uniform(
                0, final_disp_scale, size=(1, self.parameters["N_multistart"]),
            )
            final_disp_angle = np.random.uniform(
                -np.pi, np.pi, size=(1, self.parameters["N_multistart"]),
            )
        phis = np.random.uniform(
            -np.pi,
            np.pi,
            size=(  self.parameters["N_modes"],
                    self.parameters["N_blocks"], 
                    self.parameters["N_single_layer"],
                    self.parameters["N_multistart"],),
        )
        thetas = np.random.uniform(
            -1 * theta_scale,
            theta_scale,
            size=   (self.parameters["N_modes"],
                    self.parameters["N_blocks"], 
                    self.parameters["N_single_layer"],
                    self.parameters["N_multistart"],),
        )
        # if self.parameters["no_CD_end"]:
        #     betas_rho[-1] = 0
        #     betas_angle[-1] = 0
        self.betas_rho = tf.Variable(
            betas_rho, dtype=tf.float32, trainable=True, name="betas_rho",
        )
        self.betas_angle = tf.Variable(
            betas_angle, dtype=tf.float32, trainable=True, name="betas_angle",
        )
        if self.parameters["include_final_displacement"]:
            self.final_disp_rho = tf.Variable(
                final_disp_rho, dtype=tf.float32, trainable=True, name="final_disp_rho",
            )
            self.final_disp_angle = tf.Variable(
                final_disp_angle, dtype=tf.float32, trainable=True, name="final_disp_angle",
            )
        else:
            self.final_disp_rho = tf.constant(
                np.zeros(shape=((1, self.parameters["N_multistart"]))),
                dtype=tf.float32,
            )
            self.final_disp_angle = tf.constant(
                np.zeros(shape=((1, self.parameters["N_multistart"]))),
                dtype=tf.float32,
            )
        self.phis = tf.Variable(phis, dtype=tf.float32, trainable=True, name="phis",)
        self.thetas = tf.Variable(
            thetas, dtype=tf.float32, trainable=True, name="thetas",
        )

    def get_numpy_vars(
        self,
        betas_rho=None,
        betas_angle=None,
        final_disp_rho=None,
        final_disp_angle=None,
        phis=None,
        thetas=None,
    ):
        betas_rho = self.betas_rho if betas_rho is None else betas_rho
        betas_angle = self.betas_angle if betas_angle is None else betas_angle
        final_disp_rho = self.final_disp_rho if final_disp_rho is None else final_disp_rho
        final_disp_angle = self.final_disp_angle if final_disp_angle is None else final_disp_angle
        phis = self.phis if phis is None else phis
        thetas = self.thetas if thetas is None else thetas

        betas = betas_rho.numpy() * np.exp(1j * betas_angle.numpy())
        #print('-------')
      #  print(betas)
        final_disp = final_disp_rho.numpy() * np.exp(1j * final_disp_angle.numpy())
        phis = phis.numpy()
        thetas = thetas.numpy()
        # now, to wrap phis, etas, and thetas so it's in the range [-pi, pi]
        phis = (phis + np.pi) % (2 * np.pi) - np.pi
        thetas = (thetas + np.pi) % (2 * np.pi) - np.pi
        #EG: im wrapping this in range [0,2pi]
        # phis = phis  % (2 * np.pi)
        # thetas = phis  % (2 * np.pi)

        # betas
        # current have shape N_modes x N_blocks x N_multistarts
        # these will have shape N_multistart x N_modes x N_blocks
        
        # phis, thetas
        # current have shape N_modes x N_blocks x N_single_layer x N_multistarts
        # these will have shape N_multistart x N_modes x N_blocks x N_single_layer
        
        return tf.einsum("nlm->mnl", betas), final_disp.T, tf.einsum("nlsm->mnls", phis), tf.einsum("nlsm->mnls", thetas)

    def set_tf_vars(self, betas=None, final_disp=None, phis=None,thetas=None):
        # reshaping for N_multistart = 1
        # EG: Ignore for multimode case for now
        if betas is not None:
            if len(betas.shape) < 2:
                betas = betas.reshape(betas.shape + (1,))
                self.parameters["N_multistart"] = 1
            betas_rho = np.abs(betas)
            betas_angle = np.angle(betas)
            self.betas_rho = tf.Variable(
                betas_rho, dtype=tf.float32, trainable=True, name="betas_rho"
            )
            self.betas_angle = tf.Variable(
                betas_angle, dtype=tf.float32, trainable=True, name="betas_angle",
            )
        if final_disp is not None:
            if len(final_disp.shape) < 2:
                final_disp = final_disp.reshape(final_disp.shape + (1,))
                self.parameters["N_multistart"] = 1
            final_disp_rho = np.abs(final_disp)
            final_disp_angle = np.angle(final_disp)
            if self.parameters["include_final_displacement"]:
                self.final_disp_rho = tf.Variable(
                    final_disp_rho, dtype=tf.float32, trainable=True, name="final_disp_rho",
                )
                self.final_disp_angle = tf.Variable(
                    final_disp_angle, dtype=tf.float32, trainable=True, name="final_disp_angle",
                )
            else:
                self.final_disp_rho = tf.constant(
                    np.zeros(shape=((1, self.parameters["N_multistart"],))),
                    dtype=tf.float32,
                )
                self.final_disp_angle = tf.constant(
                    np.zeros(shape=((1, self.parameters["N_multistart"],))),
                    dtype=tf.float32,
                )

        if phis is not None:
            if len(phis.shape) < 2:
                phis = phis.reshape(phis.shape + (1,))
                self.parameters["N_multistart"] = 1
            self.phis = tf.Variable(
                phis, dtype=tf.float32, trainable=True, name="phis",
            )
        if thetas is not None:
            if len(thetas.shape) < 2:
                thetas = thetas.reshape(thetas.shape + (1,))
                self.parameters["N_multistart"] = 1
            self.thetas = tf.Variable(
                thetas, dtype=tf.float32, trainable=True, name="thetas",
            )

    def best_circuit(self):
        fids = self.batch_fidelities(
            self.betas_rho,
            self.betas_angle,
            self.final_disp_rho,
            self.final_disp_angle,
            self.phis,
            self.thetas,
        )
        fids = np.atleast_1d(fids.numpy())
        max_idx = np.argmax(fids)
        all_betas, all_final_disp, all_phis, all_thetas = self.get_numpy_vars(
            self.betas_rho,
            self.betas_angle,
            self.final_disp_rho,
            self.final_disp_angle,
            self.phis,
            self.thetas,
        )
        max_fid = fids[max_idx]
        betas = all_betas[max_idx]
        final_disp = all_final_disp[max_idx]
        phis = all_phis[max_idx]
        thetas = all_thetas[max_idx]
        return {
            "fidelity": max_fid,
            "betas": betas,
            "final_disp": final_disp,
            "phis": phis,
            "thetas": thetas,
        }

    def all_fidelities(self):
        fids = self.batch_fidelities(
            self.betas_rho,
            self.betas_angle,
            self.final_disp_rho,
            self.final_disp_angle,
            self.phis,
            self.thetas,
        )
        return fids.numpy()

    def best_fidelity(self):
        fids = self.batch_fidelities(
            self.betas_rho,
            self.betas_angle,
            self.final_disp_rho,
            self.final_disp_angle,
            self.phis,
            self.thetas,
        )
        max_idx = tf.argmax(fids).numpy()
        max_fid = fids[max_idx].numpy()
        return max_fid

    def print_info(self):
        best_circuit = self.best_circuit()
        with np.printoptions(precision=5, suppress=True):
            for parameter, value in self.parameters.items():
                print(parameter + ": " + str(value))
            print("filename: " + self.filename)
            print("\nBest circuit parameters found:")
            print("betas:         " + str(best_circuit["betas"]))
            print("final_disp:    " + str(best_circuit["final_disp"]))
            print("phis (deg):    " + str(best_circuit["phis"] * 180.0 / np.pi))
            print("thetas (deg):  " + str(best_circuit["thetas"] * 180.0 / np.pi))
            print("Max Fidelity:  %.6f" % best_circuit["fidelity"])
            print("\n")