import warnings
import scipy.sparse
import scipy.sparse.linalg as spalg

try:
    import pyamg
    has_pyamg = True
except ImportError:
    has_pyamg = False

from .. import cyclic
from ... import veros_method


@veros_method
def initialize_solver(vs):
    matrix = _assemble_poisson_matrix(vs)
    preconditioner = _jacobi_preconditioner(vs, matrix)
    matrix = preconditioner * matrix
    extra_args = {}

    if vs.use_amg_preconditioner:
        if has_pyamg:
            ml = pyamg.smoothed_aggregation_solver(matrix)
            extra_args["M"] = ml.aspreconditioner()
        else:
            warnings.warn("pyamg was not found, falling back to un-preconditioned CG solver")

    def scipy_solver(rhs, x0):
        rhs = rhs.flatten() * preconditioner.diagonal()
        solution, info = spalg.bicgstab(matrix, rhs,
                                        x0=x0.flatten(), tol=vs.congr_epsilon,
                                        maxiter=vs.congr_max_iterations,
                                        **extra_args)
        if info > 0:
            warnings.warn("Streamfunction solver did not converge after {} iterations".format(info))
        return solution

    vs.poisson_solver = scipy_solver


@veros_method
def solve(vs, rhs, sol, boundary_val=None):
    """
    Main solver for streamfunction. Solves a 2D Poisson equation. Uses either pyamg
    or scipy.sparse.linalg linear solvers.

    Arguments:
        rhs: Right-hand side vector
        sol: Initial guess, gets overwritten with solution
        boundary_val: Array containing values to set on boundary elements. Defaults to `sol`.
    """
    vs.flush()

    if boundary_val is None:
        boundary_val = sol

    if vs.enable_cyclic_x:
        cyclic.setcyclic_x(sol)

    boundary_mask = np.logical_and.reduce(~vs.boundary_mask, axis=2)
    rhs[...] = np.where(boundary_mask, rhs, boundary_val) # set right hand side on boundaries
    linear_solution = vs.poisson_solver(rhs, sol)
    sol[...] = boundary_val
    sol[2:-2, 2:-2] = linear_solution.reshape(vs.nx + 4, vs.ny + 4)[2:-2, 2:-2]


@veros_method
def _jacobi_preconditioner(vs, matrix):
    """
    Construct a simple Jacobi preconditioner
    """
    Z = np.ones((vs.nx + 4, vs.ny + 4))
    Y = matrix.diagonal().copy().reshape(vs.nx + 4, vs.ny + 4)[2:-2, 2:-2]
    Z[2:-2, 2:-2] = np.where(Y != 0., 1. / Y, 1.)
    try:
        Z = Z.copy2numpy()
    except AttributeError:
        pass
    return scipy.sparse.dia_matrix((Z.flatten(), 0), shape=(Z.size, Z.size)).tocsr()


@veros_method
def _assemble_poisson_matrix(vs):
    """
    Construct a sparse matrix based on the stencil for the 2D Poisson equation.
    """
    boundary_mask = np.logical_and.reduce(~vs.boundary_mask, axis=2)

    # assemble diagonals
    main_diag = np.ones((vs.nx + 4, vs.ny + 4))
    east_diag, west_diag, north_diag, south_diag = (
        np.zeros((vs.nx + 4, vs.ny + 4)) for _ in range(4))
    main_diag[2:-2, 2:-2] = -vs.hvr[3:-1, 2:-2] / vs.dxu[2:-2, np.newaxis] / vs.dxt[3:-1, np.newaxis] / vs.cosu[np.newaxis, 2:-2]**2 \
        - vs.hvr[2:-2, 2:-2] / vs.dxu[2:-2, np.newaxis] / vs.dxt[2:-2, np.newaxis] / vs.cosu[np.newaxis, 2:-2]**2 \
        - vs.hur[2:-2, 2:-2] / vs.dyu[np.newaxis, 2:-2] / vs.dyt[np.newaxis, 2:-2] * vs.cost[np.newaxis, 2:-2] / vs.cosu[np.newaxis, 2:-2] \
        - vs.hur[2:-2, 3:-1] / vs.dyu[np.newaxis, 2:-2] / vs.dyt[np.newaxis, 3:-1] * vs.cost[np.newaxis, 3:-1] / vs.cosu[np.newaxis, 2:-2]
    east_diag[2:-2, 2:-2] = vs.hvr[3:-1, 2:-2] / vs.dxu[2:-2, np.newaxis] / \
        vs.dxt[3:-1, np.newaxis] / vs.cosu[np.newaxis, 2:-2]**2
    west_diag[2:-2, 2:-2] = vs.hvr[2:-2, 2:-2] / vs.dxu[2:-2, np.newaxis] / \
        vs.dxt[2:-2, np.newaxis] / vs.cosu[np.newaxis, 2:-2]**2
    north_diag[2:-2, 2:-2] = vs.hur[2:-2, 3:-1] / vs.dyu[np.newaxis, 2:-2] / \
        vs.dyt[np.newaxis, 3:-1] * vs.cost[np.newaxis, 3:-1] / vs.cosu[np.newaxis, 2:-2]
    south_diag[2:-2, 2:-2] = vs.hur[2:-2, 2:-2] / vs.dyu[np.newaxis, 2:-2] / \
        vs.dyt[np.newaxis, 2:-2] * vs.cost[np.newaxis, 2:-2] / vs.cosu[np.newaxis, 2:-2]
    if vs.enable_cyclic_x:
        # couple edges of the domain
        wrap_diag_east, wrap_diag_west = (np.zeros((vs.nx + 4, vs.ny + 4)) for _ in range(2))
        wrap_diag_east[2, 2:-2] = west_diag[2, 2:-2] * boundary_mask[2, 2:-2]
        wrap_diag_west[-3, 2:-2] = east_diag[-3, 2:-2] * boundary_mask[-3, 2:-2]
        west_diag[2, 2:-2] = 0.
        east_diag[-3, 2:-2] = 0.

    # construct sparse matrix
    cf = tuple(diag.flatten() for diag in (boundary_mask * main_diag + (1 - boundary_mask),
                                           boundary_mask * east_diag,
                                           boundary_mask * west_diag,
                                           boundary_mask * north_diag,
                                           boundary_mask * south_diag))
    offsets = (0, -main_diag.shape[1], main_diag.shape[1], -1, 1)

    if vs.enable_cyclic_x:
        offsets += (-main_diag.shape[1] * (vs.nx - 1), main_diag.shape[1] * (vs.nx - 1))
        cf += (wrap_diag_east.flatten(), wrap_diag_west.flatten())

    if vs.backend_name == "bohrium":
        cf = np.array(cf, bohrium=False)
    else:
        cf = np.array(cf)
    return scipy.sparse.dia_matrix((cf, offsets), shape=(main_diag.size, main_diag.size)).T.tocsr()
