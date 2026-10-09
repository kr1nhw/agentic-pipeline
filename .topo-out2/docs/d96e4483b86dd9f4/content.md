# A relation-theoretic result with applications in topological algebra.pdf

<!-- doc_id: d96e4483b86dd9f4 format: pdf units: 8 -->

<!-- page: 1 kind: pdf_page route: vl_assisted -->
# A Relation-Theoretic Result with Applications in Topological Algebra*

by

A. R. BEDNAREK

University of Florida

Gainesville, Florida

and

A. D. WALLACE

University of Florida

Gainesville, Florida

§0. In this note we focus our attention on a result, which we call the Induced Function Theorem, intimately related to some work of M. L. Dubreil-Jacotin [5] and, to a lesser degree, of W. Simonsen [8]. Our statement and proof of the proposition are intended to reflect its relation-theoretic character, and most of the applications follow from a particularization of the relations involved in its statement. In general we ignore the application of the above-mentioned proposition to the derivation of numerous “isomorphism” theorems in algebra, particularly in group theory and semigroups, concentrating instead on its potential applications to the theory of machines, topologized as well as discrete. In particular, a recently proved result of Aczél and Wallace [1] is derived in a manner that we hope reflects the proposition’s utility in this area and suggests further applications.

§1. A subset R of X × Y will be called a relation from X into Y if X = Y; this will be foreshortened to a relation on X. The inverse of the relation R ⊂ X × Y is the relation from Y into X denoted by R⁻¹ and defined by R⁻¹ = {(y, x)| (x, y) ∈ R}. The relation on X defined by {(x, x)| x ∈ X} is called the diagonal of X × X and is denoted herein by diag (X × X).

If R ⊂ X × Y and if A and B are subsets of X and Y respectively, we denote by AR the set {y| (a, y) ∈ R for some a ∈ A}, while RB is defined

*The authors are grateful to the National Science Foundation (GP 3623) for its support. This paper was presented at the Conference on the Algebraic Theory of Machines, Languages and Semigroups, 29 August–8 September 1966, at Asilomar, California, organized by the Krohn-Rhodes Research Institute.

217

MATHEMATICAL SYSTEMS THEORY, Vol. 1, No. 3. Published by Springer-Verlag New York Inc.

<!-- page: 2 kind: pdf_page route: vl_assisted -->
218                    A. R. BEDNAREK AND A. D. WALLACE

dually. We shall not distinguish between xR and {x}R, preferring the former and depending upon the context to avoid ambiguity.

If R ⊂ X × Y and S ⊂ Y × Z, the composition of R and S, denoted in the sequel by R ∘ S, is the relation from X into Z given by R ∘ S = {(x, z) | (x, y) ∈ R and (y, z) ∈ S for some y ∈ Y}. In view of the preceding, the composition of R and S may be expressed by means of the set-theoretic statement R ∘ S = ⋃{Ry × yS | y ∈ Y}.

Now if R is a relation on X, the transitive closure of R, denoted by τ(R), is the relation on X defined by

$$
τ(R) = R[1] ∪ R[2] ∪ R[3] ∪ ⋯ ∪ R[n] ∪ ⋯ ,
$$

where R = R[1] and R[n] = R ∘ R[n−1] for n ≥ 2. The relation R on X is transitive if τ(R) ⊂ R. It should be observed that if R is a reflexive (diag (X × X) ⊂ R) and symmetric (R = R−1) relation on X, then the transitive closure of R is the minimal equivalence relation (reflexive, symmetric and transitive) containing R.

All of the necessary background material in topology may be found in Kelley [6] and reference to discrete semigroups is made via Clifford and Preston [4]. The expository dissertation of Paalman-de Miranda [7] is used as a general reference to the literature of topological semigroups.

§2. Basic to what follows is a lemma, the straightforward proof of which we omit.

LEMMA 1. Let X, Y and Z be all compact Hausdorff or all discrete, let f: X → Y and g: X → Z be continuous with f onto, and let

$$
f(x) = f(x′) imply g(x) = g(x′) for all x, x′ ∈ X,
$$

then there is a unique function h: Y → Z, which is continuous, satisfying g = hf.

If R is a relation from X into Y we shall denote by p and q the first and second projections of R into X and Y respectively.

THEOREM 1. (Induced Function Theorem). Let X and Y be both compact Hausdorff or both discrete, let R be a closed relation from X into Y and let E and F be closed equivalences on X and Y respectively. If X = RY and if R−1 ∘ E ∘ R ⊂ F then there exists a unique function h: X/E → Y/F, which is continuous, for which the following diagram is analytic.

![]()

Moreover, if, in addition to the previous hypotheses, Y = XR and R ∘ F ∘ R−1 ⊂ E, then h is a homeomorphism.

Proof. Denoting the natural maps by φ and ψ, that is, φ: X → X/E and

<!-- page: 3 kind: pdf_page route: vl_assisted -->
# A Relation-Theoretic Result with Applications in Topological Algebra

219

ψ: Y → Y/F, we note that in view of the assumption that X = RY the composite map φp is onto.

Now if φp(x, y) = φp(x', y') for (x, y) and (x', y') in R, then (x, x') ∈ E and hence

$$
(y, y') = (y, x) ∘ (x, x') ∘ (x', y') ∈ R⁻¹ ∘ E ∘ R ⊂ F.
$$

This implies that ψ(y) = ψ(y') or, equivalently,

$$
ψq(x, y) = ψq(x', y').
$$

The existence of the map h follows directly from the preceding lemma.

In a like manner, if in addition Y = XR and R ∘ F ∘ R⁻¹ ⊂ E, then there exists a unique map g: Y/F → X/E such that h and g are mutually inverse, therefore h is a homeomorphism.

Remark. Actually it would have been sufficient to assume in the preceding that Ex ∩ RY ≠ □ for each x ∈ X, a condition which is satisfied if X = RY.

We now turn our attention to a number of corollaries to the Induced Function Theorem (IFT), the first of these being a result contained implicitly in the paper of Simonsen [8].

COROLLARY 1. Let R be a relation from X into Y satisfying X = RY and Y = XR. If E and F are the minimal equivalences containing R ∘ R⁻¹ and R⁻¹ ∘ R respectively, then there exists a unique one-to-one and onto map f: X/E → Y/F such that the diagram below is analytic.

![]()

Proof. This follows directly from the induced function theorem after observing that if X = RY, the relation R ∘ R⁻¹ is reflexive as well as symmetric, so that E is precisely the transitive closure of R ∘ R⁻¹. Similarly, F is the transitive closure of R⁻¹ ∘ R so that

$$
R⁻¹ ∘ E ∘ R = R⁻¹ ∘ τ(R ∘ R⁻¹) ∘ R ⊂ τ(R⁻¹ ∘ R) = F
$$

and

$$
R ∘ F ∘ R⁻¹ = R ∘ τ(R⁻¹ ∘ R) ∘ R⁻¹ ⊂ τ(R ∘ R⁻¹) = E.
$$

The following corollary is equivalent to the preceding, as can be seen from the way in which it is derived.

COROLLARY 2. Let R ⊂ X × Y and S ⊂ Y × X be such that X = RY and Y = XS. If E and F are the minimal equivalences containing R ∘ S and S ∘ R respectively, then there exists a one-to-one map f of X/E onto Y/F.

<!-- page: 4 kind: pdf_page route: vl_assisted -->
220 A. R. BEDNAREK AND A. D. WALLACE

Proof. Define the relation T ⊂ X × Y by

$$
T = R ∪ S⁻¹.
$$

Note that X = TY and Y = XT. The conclusion follows from Corollary 1 by observing that the minimal equivalence containing T ∘ T⁻¹ is exactly the minimal equivalence containing R ∘ S, and similarly for T⁻¹ ∘ T and S ∘ R.

Now

$$
T ∘ T⁻¹ = (R ∪ S⁻¹) ∘ (R ∪ S⁻¹)⁻¹ = (R ∪ S⁻¹) ∘ (R⁻¹ ∪ S)
$$

$$
= (R ∘ R⁻¹) ∪ (S⁻¹ ∘ R⁻¹) ∪ (S⁻¹ ∘ S) ∪ (R ∘ S)
$$

so that it is clear that the minimal equivalence containing T ∘ T⁻¹, call it E′, contains E. To show the opposite inclusion, note first that (R ∘ S) and (S⁻¹ ∘ R⁻¹) = (R ∘ S)⁻¹ are contained in E. Suppose (x, z) ∈ (R ∘ R⁻¹), then for some y ∈ Y we have (x, y) and (z, y) contained in R. Since Y = SX there is a w ∈ X such that (y, w) ∈ S, and thus (x, w) and (z, w) are elements of R ∘ S. However this implies that (x, w), (z, w) ∈ E so that (x, z) ∈ E. The inclusion S⁻¹ ∘ S ⊂ E is obtained in a similar way, completing the argument that E = E′.

A completely dual argument for F establishes the desired conclusion.

Of course, Corollary 1 follows from Corollary 2 by taking S = R⁻¹.

We revert to topological assumptions to obtain the following.

COROLLARY 3. Let X, Y₁ and Y₂ be all compact Hausdorff or all discrete. For i = 1, 2, let Rᵢ be a closed relation from X into Yᵢ, and let Eᵢ and Fᵢ be closed equivalences on X and Yᵢ, respectively satisfying

$$
X = RᵢYᵢ, Yᵢ = XRᵢ, Rᵢ⁻¹ ∘ Eᵢ ∘ Rᵢ ⊂ Fᵢ
$$

and Rᵢ ∘ Fᵢ ∘ Rᵢ⁻¹ ⊂ Eᵢ. Suppose furthermore that R₁⁻¹ ∘ E₂ ∘ R₁ ⊂ F₁ and R₂⁻¹ ∘ E₁ ∘ R₂ ⊂ F₂, then Y₁/F₁ and Y₂/F₂ are topologically equivalent.

Proof. Define the relation P from Y₁ into Y₂ by P = R₁⁻¹ ∘ R₂. If y ∈ Y₁, then there is an x₀ ∈ X such that (x₀, y) ∈ R₁. Now X = R₂Y₂ implies there exists a y′ ∈ Y₂ such that (x₀, y′) ∈ R₂. Thus

$$
(y, y′) ∈ R₁⁻¹ ∘ R₂ = P,
$$

so that y ∈ PY₂. In a similar way we may show that Y₂ = Y₁P.

Considering P⁻¹ ∘ F₁ ∘ P, we see that

$$
P⁻¹ ∘ F₁ ∘ P = (R₁⁻¹ ∘ R₂)⁻¹ ∘ F₁ ∘ (R₁⁻¹ ∘ R₂)
$$

$$
= R₂⁻¹ ∘ (R₁ ∘ F₁ ∘ R₁⁻¹) ∘ R₂
$$

$$
⊂ R₂⁻¹ ∘ E₁ ∘ R₂ ⊂ F₂
$$

and, similarly, P ∘ F₂ ∘ P⁻¹ ⊂ F₁. Thus all of the hypotheses of (IFT) are satisfied so that there is a homeomorphism from Y₁/F₁ onto Y₂/F₂.

Remark. It may be of some interest to note the diagram corresponding to Corollary 3. With ≅ denoting topological equivalence, the hypotheses of the corollary guarantee that X/E₁ ≅ Y/F₁, X/E₂ ≅ Y/F₂ and, as we have

<!-- page: 5 kind: pdf_page route: vl_assisted -->
# A Relation-Theoretic Result with Applications in Topological Algebra

221

seen, Y/F₁ ≅ Y/F₂. Thus all of the nodes on the upper “face” of the diagram are topologically equivalent.

![]()

§3. We focus our attention on applications of the preceding results. As mentioned earlier our principal concern here is with applications to topological algebra, more specifically to topologized “machines”.

A recursion is a continuous function

$$
T × X → X
$$

such that T and X are nonvoid Hausdorff spaces. The value of the function at (t, x) is denoted by tx. If T is a groupoid, its multiplication will also be denoted by juxtaposition.

A recursion is called discrete [compact] if both T and X are discrete [compact].

If, in the recursion T × X → X, T′ ⊂ T and X′ ⊂ X, then

$$
T′X′ = {tx | t ∈ T′ and x ∈ X′}
$$

and T′x is used in place of T′{x}.

THEOREM 2. Let T × X → X and T × Y → Y be both compact or both discrete recursions. Suppose that Tx₀ = X and Ty₀ = Y for some x₀ ∈ X and y₀ ∈ Y. If, for all t, t′ ∈ T,

$$
(*)    tx₀ = t′x₀ if and only if ty₀ = t′y₀,
$$

then X ≅ Y.

Proof. Define R₁ ⊂ T × X and R₂ ⊂ T × Y by R₁ = {(t, x) | tx₀ = x} and R₂ = {(t, y) | ty₀ = y}. Let E₁ = R₁ ∘ R₁⁻¹, F₁ = R₁⁻¹ ∘ R₁, E₂ = R₂ ∘ R₂⁻¹ and F₂ = R₂⁻¹ ∘ R₂. If we observe that diag (X × X) = F₁, diag (Y × Y) = F₂ and that (*) implies E₁ = E₂, it is immediate that all of the hypotheses of Corollary 3 are satisfied so that

$$
X ≅ X/diag (X × X) ≅ Y/diag (Y × Y) ≅ Y.
$$

As a second application of (IFT) we consider the case in which, rather than having a recursion, we have a semigroup T, a space X and a relation R from T × X into X. Given a congruence E on T and an equivalence F on X we are interested in relation-theoretic conditions under which we may generate a topological machine, T/E × X/F → X/F.

<!-- page: 6 kind: pdf_page route: vl_assisted -->
222    A. R. BEDNAREK AND A. D. WALLACE

Recall that a semigroup (topological) is a nonvoid Hausdorff space with a continuous associative multiplication. If T is a semigroup and if E is an equivalence on T, then E is called a congruence if (t₁, t₂) ∈ E implies that (tt₁, tt₂) and (t₁t, t₂t) are contained in E for each t ∈ T.

By topological machine we mean a continuous function

$$
T × X → X
$$

such that T is a semigroup, X is a nonvoid Hausdorff space and

$$
t₁(t₂x) = (t₁t₂)x for all t₁, t₂ ∈ T and all x ∈ X.
$$

If E is an equivalence on T and F is an equivalence on X, then we define the product of E and F, denoted by E × F, to be the relation on T × X defined by ((t, x), (t', x')) ∈ E × F if and only if (t, t') ∈ E and (x, x') ∈ F. As noted in Bourbaki [3, p. 122], E × F is an equivalence relation on T × X.

Throughout the remainder of this section we use φ, ψ and η to denote the natural maps φ: T → T/E, ψ: X → X/F and η: T × X → T × X/E × F. As a direct consequent of Lemma 1, we have:

LEMMA 2. If T and X are both compact Hausdorff or both discrete and if E and F are closed equivalences on T and X respectively, then there exists a unique homeomorphism h: T/E × X/F → T × X/E × F for which h(φ × ψ) = η.

THEOREM 3. Let T and X be both compact Hausdorff or both discrete and let T be, in addition, a semigroup. If R is a closed relation from T × X into X, E is a closed congruence on T and F is a closed equivalence satisfying

$$
(i)    T × X = RX
$$

$$
(ii)   R⁻¹ ∘ (E × F) ∘ R ⊂ F
$$

$$
(iii)  (t₁t₂, x)R × (t₁, z)R ⊂ F
$$

for all t₁, t₂ ∈ T and all z ∈ (t₂, x)R, then there exists a unique continuous function T/E × X/F → X/F for which T/E × X/F → X/F is a topological machine and the diagram below is analytic.

![]()

Proof. Using (i), (ii), and Theorem 1, we have that there exists a unique continuous function h: T × X/E × F → X/F for which the diagram

![]()

<!-- page: 7 kind: pdf_page route: vl_assisted -->
# A Relation-Theoretic Result with Applications in Topological Algebra

223

is analytic. By Lemma 2, there is a unique homeomorphism h': T/E × X/F → T × X/E × F for which h'(φ × ψ) = η. Let f: T/E × X/F → X/F be defined by f = hh'.

Since T/E is a semigroup we need only show that

$$
f(φ(t₁)φ(t₂), ψ(x)) = f(φ(t₁), f(φ(t₂), ψ(x)))
$$

for all t₁, t₂ ∈ T and for each x ∈ X.

Now f(φ(t₁)φ(t₂), ψ(x)) = f(φ(t₁t₂), ψ(x)) = f(φ × ψ)(t₁t₂, x). Since T × X = RX, there is an x₀ ∈ X such that ((t₁t₂, x), x₀) ∈ R, so that

$$
f(φ(t₁)φ(t₂), ψ(x)) = f(φ × ψ)p((t₁t₂, x), x₀) = ψq((t₁t₂, x), x₀) = ψ(x₀).
$$

But f(φ(t₁), f(φ(t₂), ψ(x))) = f(φ(t₁), f(φ × ψ)(t₂, x)), and since T × X = RX we may find x₁, x₂ ∈ X such that ((t₂, x), x₁) ∈ R and ((t₁, x₁), x₂) ∈ R. Then

$$
f(φ(t₁), f(φ(t₂), ψ(x))) = f(φ(t₁), f(φ × ψ)p((t₂, x), x₁))
$$

$$
= f(φ × ψ)p((t₁, x₁), x₂) = ψ(x₂).
$$

However by (iii), (x₀, x₂) ∈ (t₁t₂, x)R × (t₁, x₁)R, where x₁ ∈ (t₂, x)R, and hence ψ(x₀) = ψ(x₂), completing the proof.

A special case of the preceding was proved in [2] where one begins with a compact or discrete topological machine T × X → X, a closed congruence E on T and a closed equivalence F on X satisfying EF ⊂ F (the multiplication being coordinate-wise). There, of course, the relation R is taken to be the “graph” of the function T × X → X, that is,

$$
R = {((t, x), y) | tx = y}.
$$

Our final formal application of (IFT) involves the derivation of a recently proved result of Aczél and Wallace [1] which concerns itself with conditions on a recursion which force the state space to admit a particular algebraic structure.

THEOREM 4. (Aczél-Wallace). Let T × X → X be a compact or discrete recursion satisfying (i) Tz = X for some z ∈ X and (ii) t₁(t₂x) = t₂(t₁x) for all t₁, t₂ ∈ T and for each x ∈ X. Then X is a commutative semigroup under an operation ∗ with z as unit such that tx = (tz) ∗ x for each t ∈ T and each x ∈ X.

Proof. Defining the relation R from T into X by (t, x) ∈ R if and only if tz = x, we see that T = RX and X = TR. Now E = R ∘ R⁻¹ = {(t, t') | tz = t'z} is an equivalence. Similarly, F = R⁻¹ ∘ R = diag (X × X), so that by Corollary 1 to Theorem 1 there exists a unique one-to-one and onto function f: T/E → X/diag (X × X) for which fφp = ψq.

On the topological side, the relation R being closed, it follows that E is also, so that f is a homeomorphism. Denote the inverse of f by g.

To complete the proof we show that T/E admits a continuous, commutative, associative multiplication ∘ having gψ(z) as unit. Observing that (t, t') ∈ E implies tx = t'x for all x ∈ X, by Lemma 1 there is a unique continuous map ∘: T/E × T/E → T/E such that the diagram below is analytic.

<!-- page: 8 kind: pdf_page route: vl_assisted -->
224                                                                     A. R. BEDNAREK AND A. D. WALLACE

$$
T/E × T/E → T/E
(φ × gψ) ↓        ↑ gψ
T × X → X
$$

Since (t, tz) ∈ R for each t ∈ T, we have gψ(tz) = φ(t). Using this we note that

$$
φ(t₁) ∘ φ(t₂) = φ(t₁) ∘ gψ(t₂z) = gψ(t₁(t₂z))
= gψ(t₂(t₁z)) = φ(t₂) ∘ φ(t₁),
$$

that is, the operation ∘ is commutative. Now

$$
φ(t₁) ∘ (φ(t₂) ∘ φ(t₃)) = φ(t₁) ∘ gψ(t₂(t₃z))
= g(t₁(t₂(t₃z))).
$$

Similarly, (φ(t₁) ∘ φ(t₂)) ∘ φ(t₃) = g(t₃(t₁(t₂z))) from which it follows, since t(t'x) = t'(tx) for all t, t' ∈ T and all x ∈ X, that

$$
φ(t₁) ∘ (φ(t₂) ∘ φ(t₃)) = (φ(t₁) ∘ φ(t₂)) ∘ φ(t₃).
$$

Let t₀ ∈ T be such that t₀z = z; then for any t ∈ T, φ(t) ∘ φ(t₀) = gψ(t(t₀z)) = gψ(tz) = φ(t), so that φ(t₀) = gψ(t₀z) = gψ(z) is a unit for T/E.

We have restricted ourselves in the preceding to applications concerning, primarily, abstract machines, topologized as well as discrete. It is clear that the propositions considered may be applied to generate many of the classical isomorphism theorems in algebra. In addition the topologized and relativized version [9] of the Schützenberger-Preston Theorem can be obtained directly from Corollary 3, but the introduction of the notation and concepts to verify this formally would have led us too far afield from the original intent of this note.

# REFERENCES

[1] J. ACZÉL and A. D. WALLACE, A note on generalizations of transitive systems of transformations, Colloq. Math. (to appear).

[2] A. R. BEDNAREK and A. D. WALLACE, Finite approximants of compact totally disconnected machines, Math. Systems Theory 1 (1967), 209–216.

[3] N. BOURBAKI, Théorie des Ensembles, Livre I, Hermann, Paris, 1960.

[4] A. H. CLIFFORD and G. B. PRESTON, The Algebraic Theory of Semigroups, Amer. Math. Soc. Surveys, No. 7, Providence, R.I., 1961.

[5] M. L. DUBREIL-JACOTIN, Applications multiformes et relations d'équivalences, C. R. Acad. Sci. Paris 230 (1950), 906–908.

[6] J. L. KELLEY, General Topology, Van Nostrand, Princeton, N.J., 1955.

[7] A. B. PAALMAN-DE MIRANDA, Topological Semigroups, Mathematical Centre Tracts, Amsterdam, 1964.

[8] W. SIMONSEN, Sur les correspondances multivoques entre deux ensembles abstraits, Acta Math. 81 (1949), 291–297.

[9] A. D. WALLACE, Relative ideals in semigroups. II., Acta Math. Acad. Sci. Hungar. 14 (1963), 137–148.

(Received 30 November 1966)

