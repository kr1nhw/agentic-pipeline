# Chucky- A Succinct Cuckoo Filter for LSM-Tree.pdf

<!-- doc_id: 55faa3e3d8587040 format: pdf units: 14 -->

<!-- page: 1 kind: pdf_page route: static -->
# Chucky: A Succinct Cuckoo Filter for LSM-Tree

## Niv Dayan, Moshe Twitto

### ABSTRACT

### 1 INTRODUCTION

Pliops

ABSTRACT                                                                                     query cost                                                                              r w ilte el Ii Dth s
                                                                                                                                                       false positive rate      oo rF Lev
Modern key-value stores typically rely on an LSM-tree in storage                                 tiering        da sit za e                                                 Cu nc tk ege
(SSD) to handle writes and Bloom filters in memory (DRAM) to                                 Chucky       le state-of-the-art                                                 I
optimize reads. With ongoing advances in SSD technology shrink-
ing the performance gap between storage and memory devices, the                                            velin Bloom filters                                                    state-of-the-art
Bloom filters are now emerging as a performance bottleneck.                                                      g                                                                  Bloom filters

We propose Chucky, a new design that replaces the multiple                                                                                                                         Chucky
Bloom filters by a single Cuckoo filter that maps each data entry to
an auxiliary address of its location within the LSM-tree. We show                            (A) construction cost                                                          (B) data size
that while such a design entails fewer memory accesses than with
Bloom filters, its false positive rate off the bat is higher. The reason                     Figure 1: Existing filters for LSM-tree can not scale query
is that the auxiliary addresses occupy bits that would otherwise be                          cost, construction cost and the false positive rate all at once.
used as parts of the Cuckoo filter’s fingerprints. To address this, we
harness techniques from information theory to succinctly encode                              run can approach and even exceed the latency of the SSD I/O(s)
the auxiliary addresses so that the fingerprints can stay large. As a                        that fetch the target entry from storage.
result, Chucky achieves the best of both worlds: a modest access                             Problem 2: Workload Skew. KV-stores typically maintain an in-
cost and a low false positive rate at the same time.                                         memory block cache to store frequently accessed data blocks and
                                                                                             thereby optimize for skew, which is often common [41]. Querying
ACM Reference Format:                                                                        a block cache still requires to first traverse potentially all the Bloom
Niv Dayan, Moshe Twitto. 2021. Chucky: A Succinct Cuckoo Filter for LSM-                     filters to identify the run that contains the target entry. As there
Tree . In Proceedings of the 2021 International Conference on Management of                  are no storage I/Os in this case, the cost of traversing the Bloom
Data (SIGMOD ’21), June 20–25, 2021, Virtual Event, China. ACM, New York,                    filters becomes even more dominant.
NY, USA, 14 pages. https://doi.org/10.1145/3448016.3457273                                   Problem 3: Read vs. Write Contention. To mitigate the Bloom
                                                                                             filters’ query cost, one can tune the LSM-tree to merge more fre-
1 INTRODUCTION                                                                               quently so that there are fewer runs and thus fewer Bloom filters
                                                                                             to access. This, however, increases the Bloom filters’ construction
Modern KV-stores rely on an LSM-tree to persist data in storage. An                          costs. A Bloom filter is immutable and has to be rebuilt from scratch
LSM-tree buffers new data in memory, flushes the buffer to storage                           during each compaction. Compacting more frequently, therefore,
as a sorted run whenever it fills up, and compacts runs across a                             entails rebuilding Bloom filters more frequently. It was recently
logarithmic number of levels [79]. To optimize application point                             reported that Bloom filter construction can amount to over 70% of
queries, there is an in-memory Bloom filter [11] for each run to rule                        performance overheads on the write path [58]. Thus, the access
out runs that do not contain a target entry. Such designs are used                           and construction costs of Bloom filters contend with each other in
in OLTP [45], HTAP [71], social graphs [74], FTL design [12, 27],                            an LSM-tree context, as depicted conceptually in Figure 1 Part (A).
data series [61–63], blockchain [34], stream-processing [21], etc.                           Problem 4: Scalability with Data Size. As the data size grows,
Problem 1: Changing Storage Media. LSM-tree was originally                                   more Bloom filters need to be queried and constructed across more
designed for HDDs, which are 5-6 orders of magnitude slower than                             LSM-tree levels. Hence, the overheads of querying and construct-
DRAM memory chips. The advent of SSDs, however, has shrunk                                   ing Bloom filters grow too. As shown in Figure 1 Part (A), this
the performance gap between storage and memory to 2-3 orders                                 causes the read vs. write trade-off curve to move outwards and
of magnitude [10, 30, 37]. Today, a memory I/O takes ≈ 100 ns                                leave applications with worse trade-offs to choose between. With
while a read I/O on, say, an Intel Optane SSD takes ≈ 10 µs. Hence,                          data growing exponentially across many modern applications, the
memory access is no longer negligible relative to storage access.                            outcome is rapidly deteriorating performance.
For LSM-trees, especially modern designs with tens to hundreds of                            Research Question. Can we devise a replacement for Bloom filters
runs [59, 76, 85, 86, 98], querying a Bloom filter at ≈ 100 ns for each                      that exhibits more robust and scalable performance for LSM-tree
                                                                                             with respect to (1) storage media, (2) workload skew, (3) LSM-tree
Permission to make digital or hard copies of all or part of this work for personal or        tuning, and (4) data size?
classroom use is granted without fee provided that copies are not made or distributed        Cuckoo Filter: The Promise. Over the past decade, a new family
for profit or commercial advantage and that copies bear this notice and the full citation    of data structures emerged as an alternative to Bloom filters. These
on the first page. Copyrights for components of this work owned by others than ACM           structures operate by storing a small hash digest called a fingerprint
must be honored. Abstracting with credit is permitted. To copy otherwise, or republish,      for every entry’s key within a compact hash table. They include
to post on servers or to redistribute to lists, requires prior specific permission and/or a  Quotient filter [9, 81], Cuckoo filter [39] and others [15, 44, 80, 96].
fee. Request permissions from permissions@acm.org.
SIGMOD ’21, June 20–25, 2021, Virtual Event, China                                              In this paper, we replace an LSM-tree’s multiple Bloom filters
© 2021 Association for Computing Machinery.                                                  by a Cuckoo filter variant that maps each data entry to both a
ACM ISBN 978-1-4503-8343-1/21/06. . . $15.00
https://doi.org/10.1145/3448016.3457273

<!-- page: 2 kind: pdf_page route: static -->
| Leveling Lazy-Leveling Tiering
1 123 1 1 23
Z=1 Z=1 Z=T-1
slevel
2 K=1 K=T-1 4 5 6 K=T-1 2 4 5 6
T=4 T=4 T=4
3 7 7 8 9 3
more read & more write
space optimized optimized | Term | Definition |
| --- | --- | --- |
|  | N | largest level size (entries) |
|  | P | buffer size (entries) |
|  | T | LSM-tree size ratio |
|  | L | number of LSM-tree levels |
|  | Z | number of sub-levels at largest level |
|  | K | number of sub-levels at each of Levels 1 to L-1 |
|  | Ai | number of sub-levels at Level i |
|  | A | number of sub-levels in the whole LSM-tree |
|  | M | filtering memory budget (bits/entry) |

# fingerprint and to an auxiliary Level ID (LID). A LID is a small 2 STATE-OF-THE-ART: LSM & BLOOM

Leveling  Lazy-Leveling                Tiering                   Term  Definition
                                                                               N  largest level size (entries)
        1  1                   123                123                          P  buffer size (entries)
                                          Z=1                Z=T-1             T  LSM-tree size ratio
levels        Z=1                                                              L  number of LSM-tree levels
                                                                               Z  number of sub-levels at largest level
        2  2 K=1               4 5 6 K=T-1        4 5 6 K=T-1                  K  number of sub-levels at each of Levels 1 to L-1
              T=4                 T=4                      T=4                Ai  number of sub-levels at Level i
                                                                               A  number of sub-levels in the whole LSM-tree
        3  3                   7               7  8        9                  M   filtering memory budget (bits/entry)

more read &       more write
              space optimized     optimized

fingerprint and to an auxiliary Level ID (LID). A LID is a small            2 STATE-OF-THE-ART: LSM & BLOOM
string of bits that identifies where a given entry resides within the
LSM-tree. It points to the level (or part of a level) that needs to be      LSM-tree consists of multiple levels of exponentially increasing
searched during a point read for a matching fingerprint within the
Cuckoo filter. The promise is to reduce filtering memory I/Os to a          capacities. Level 0 is an in-memory buffer (a.k.a memtable), typically
small and constant number, an improvement over Bloom filters.
Scaling the False Positive Rate. Despite the promise of this ap-            implemented as a skip list or a hash table. All other levels are in
proach, we also identify a challenge: keeping the false positive rate
(FPR) low and stable. The FPR is inversely related to the fingerprint       storage. The application inserts key-value entries into the buffer.
size. Under a constrained memory budget, the LIDs occupy bits that
would otherwise be used as parts of the fingerprints. Worse, as the         When the buffer fills up, it gets flushed to storage.
data grows, the number of levels in the LSM-tree grows too. This
requires increasing the LID size (in bits) to still be able to identify     Merge Policy. An LSM-tree’s merge policy dictates which data to
every level (or part thereof). As a result, the LIDs “steal” more bits      merge in storage and when. While merge policies can be formalized
from the fingerprints as data grows. This causes the FPR to increase
over time. The outcome is more storage I/Os, which harm perfor-             in different ways, we adopt the Dostoevsky framework [28] as it
mance. We illustrate the challenge conceptually in Figure 1 Part (B)
using the curve labeled Cuckoo Filter with Integer Level IDs.               generalizes several well-known policies. The number of levels L is
Insight: Level ID Compressibility. Our core insight in this paper           ⌈logT (N/P)⌉, where N is the number of entries at the largest level,
is that, fortunately, LIDs are extremely compressible. The reason           P is the buffer size, and T is the capacity ratio between any two
is the LSM-tree’s exponential growth; most entries reside at larger         adjacent levels (T ≥2). Each level consists of one or more sub-levels,
levels. Hence, the distribution of LIDs within the Cuckoo filter is
heavily skewed: most LIDs correspond to the largest level while             where a sub-level is a placeholder for one chunk of sorted data. At
exponentially fewer correspond to smaller levels. We can therefore
encode the LIDs of larger levels with fewer bits than the LIDs of           the largest level, there are Z sub-levels (1≤ Z <T ). At each of the
smaller levels to minimize the average LID size. The saved bits can         smaller levels, there are K sub-levels (1 ≤ K < T ). Figure 2 shows
be assigned to the fingerprints to keep them large.
Chucky. We present Chucky: Huffman Coded Key-Value Store, a                 how sub-levels are numbered for different configurations of the
new design that scales memory I/Os, memory footprint, and the               parameters Z and K. The capacity at Level i is P · T i entries, and
false positive rate at the same time. It achieves this by replacing         it is equally divided among the sub-levels at Level i. Equation 1
the Bloom filters by a Cuckoo filter with succinctly encoded (i.e.,         denotes Ai as the number of sub-levels at Level i and A as the overall
compressed) LIDs. We explore in detail the design space for LID             number of sub-levels. The number of levels L and the number of
compression, and we identify and tackle the resulting challenges: (1)       sub-levels A both grow with the data size.
how to align fingerprints and compressed LIDs within the Cuckoo
filter’s buckets, and (2) how to en/decode LIDs efficiently. We use         Ai = K for 1 ≤ i < L           L
the saved bits to keep the fingerprints large and thus to keep the                  Z else
FPR low and stable as the data grows.                                                             A = Ai = (L − 1) · K + Z (1)
Contributions. Our contributions are as follows.
                                                                                                         i =1
   (1) We show that the Bloom filters of LSM-tree are emerging as
        a memory I/O bottleneck as SSDs evolve and get faster.              There is zero or one run at each sub-level. A run comprises key-

(2) We replace the Bloom filters by a Cuckoo filter that maps            value entries sorted by key. Different runs may have overlapping
        data entries to their locations within the LSM-tree.
                                                                            key ranges. Each run may further be divided into smaller files
   (3) We show that level IDs are extremely compressible. We study
        how to to efficiently en/decode them using Huffman coding.          with non-overlapping key ranges called Sorted String Tables (SSTs),

(4) We show how to align compressed level IDs and fingerprints           though we will use run as the unit of data in the paper.
        within Cuckoo filter buckets to ensure good space utilization.
                                                                               When all sub-levels at Level i have reached capacity, their con-
   (5) We show experimentally that Chucky scales memory band-               stituent runs get merged into the highest sub-level at Level i +1 that
        width, memory footprint, and the FPR at the same time.              is below capacity. If there is already a run at this target sub-level, it

is included in the merge. Hence, the jth youngest run at Level i is
                                                                            always at sub-level number (i − 1) · K + j.

The parameters K and Z can be co-tuned to assume different

trade-offs. Figure 2 shows how to tune them to assume three merge

policies: (1) leveling, best for range reads, (2) tiering, best for writes,
                                                                            and (3) lazy leveling, best for point reads. The size ratio T can be
                                                                            tweaked to fine-tune these trade-offs, though Figure 2 fixes it to four.
                                                                            When the size ratio T is set to two, its lowest possible setting, the
                                                                            three merge policies behave identically. As we increase T with each
                                                                            policy, their behaviors diverge. With a vision towards navigable

systems that can learn and adapt across a wide design space to

optimize for different workloads [5, 6, 47, 52–57], we design Chucky

to span this entire wide compaction design space.

Updates & Deletes. Updates and deletes are performed out-of-
                                                                            place by inserting a key-value entry with the updated value into

2

Figure 2: LSM-tree variants and terms used to describe them throughout the paper. Each rectangle represents a sub-level.

<!-- page: 3 kind: pdf_page route: static -->
# have either a true or a false positive. The false positive probability 3 PROMISE: LSM-TREE & CUCKOO FILTER

Leveling Lazy-Leveling Tiering                       there are fewer BFs while construction cost increases as the BFs get

application query O(L)   O(L · T ) O(L · T )                                  rebuilt more frequently. Can we better scale these costs with respect
                                                                              to the data size while also alleviating their read/write contention?
application update O(L · T ) O(L + T )        O (L)                           False Positive Rate Analysis. We define the false positive rate
                                                                              (FPR) as the sum of FPPs across all filters. The FPR expresses the
Table 1: Blocked Bloom filters’ memory I/O complexities.                      average number of I/Os due to false positives that occur per point
                                                                              query over the whole LSM-tree. Equation 2 expresses the FPR for
the buffer (for a delete, the value is a tombstone). Whenever runs

that contain entries with the same key are merged, older versions

are discarded and only the newest version is kept. To always find             most KV-stores, which assign the same number of bits per entry

the most recent version of an entry, a query traverses the runs from          to all their BFs. This approach, however, was recently deemed sub-
youngest to oldest (from smaller to larger levels, and from lower             optimal. The optimal approach is to reassign ≈ 1 bit per entry from
to higher sub-levels within a level). It terminates when it finds the         the largest level and to use it to assign linearly more bits per entry
first entry with a matching key. If this entry’s value is a tombstone,
the query returns a negative result.                                          to filters at smaller levels [25, 26]. While this increases the largest
Fence Pointers. For each run, there are fence pointers in memory
that comprise the min/max key at every data block. They allow                 level’s FPP, it exponentially decreases the FPPs at smaller levels
queries to binary search for the relevant data block that contains
a given key in ≈ log(N ) memory I/Os so that this block can be                such that the overall FPR is lower, as expressed in Equation 3 [28].
retrieved cheaply with one storage I/O.
Bloom Filters. For each run in the LSM-tree, there is an in-memory            F P Runi f or m = 2−M ·ln(2) · (K · (L − 1) + Z )          (2)
Bloom filter (BF), a space-efficient probabilistic data structure used
                                                                                                                                 T
                                                                                                              1 T T −1
                                                                              F P Roptimal = 2−M ·ln(2) T −1
                                                                                                ·Z T ·KT ·                               (3)
                                                                                                              T −1

Equation 3 states that with the optimal approach, the relationship

between memory and FPR is independent of the number of levels

and thus of data size, unlike Equation 2. The reason is that as the

to test whether a key is a member of a set [11]. A BF is an array of          LSM-tree grows, smaller levels are assigned exponentially lower
                                                                              FPRs thus causing the sum of FPRs to converge. It is imperative
bits with h hash functions. Each key is mapped using these hash               that any replacement we devise for the LSM-tree’s Bloom filters
functions to h random bits, setting them from 0 to 1 or keeping them          either matches or improves on the FPR expressed in Equation 3.

set to 1. Checking for the existence of a key requires examining its          3 PROMISE: LSM-TREE & CUCKOO FILTER

h bits. If any are set to 0, we have a negative. If all are set to 1, we      Cuckoo filter [39] (CF) is one of several data structures [9, 15, 44,
have either a true or a false positive. The false positive probability
(FPP) is 2−M ·ln(2), where M is the number of bits per entry. As we           81, 96] that recently emerged as alternatives to Bloom filters. In
increase M, the probability of hash collisions decreases and so the
                                                                              their core, these structures all employ a compact hash table that
FPP drops. A BF does not support range reads or deletes. The lack             stores fingerprints of keys, where a fingerprint is a string of F bits
                                                                              derived by hashing a key. CF comprises an array of buckets, each
of delete support means that a new BF has to be built from scratch            with S slots for fingerprints. During insertion, an entry with key k
                                                                              is hashed to two bucket addresses b1 and b2 using Equation 4. A
for every new run as a result of compaction.                                  fingerprint of key k is inserted into whichever bucket has space. If
                                                                              both buckets are full, however, some fingerprint from one of the two
   A BF entails h memory I/Os for an insertion or for a query to an
existing key. For a query to a non-existing key, it entails on average
two memory I/Os since ≈ 50% of the bits are set to zero and so the

expected number of bits checked before incurring a zero is two.

Blocked Bloom Filters. To optimize memory I/Os, Blocked Bloom                 buckets is randomly chosen and swapped to its alternative bucket
filter has been proposed as an array of contiguous BFs, each the size
                                                                              to clear space. By virtue of using the xor operator, the right-hand

of a cache line [66, 84]. A key is inserted by first hashing it to one of     side of Equation 4 allows to always compute an entry’s alternative

the constituent BFs and then inserting the key into it. This entails          bucket using the fingerprint and current bucket address without

only one memory I/O for any insertion or query. The trade-off is              the original key. The swapping process continues recursively either

a slight FPP increase. RocksDB has adopted blocked BFs. We use                until a free slot is found for all fingerprints or until a swapping

standard and blocked BFs as baselines in this paper, and we focus             threshold is reached, at which point the insertion fails.

more on blocked BFs as they are the tougher competition.                      b1 = hash(k )     b2 = b1 ⊕ hash(k′s f inдer pr int ) (4)

Memory I/O Analysis. With blocked BFs, the overall cost of a                     We employ a CF with S set to four slots per bucket through the
point query is (L − 1) · K + Z memory I/Os, one for each sub-                 paper. Such a tuning can reach 95% space utilization with high

level of the LSM-tree. The cost of an insertion/update/delete, on

the other hand, is the same as the LSM-tree’s write-amplification:            probability without incurring insertion failures and with only 1-2
                                                                              amortized swaps per insertion. The false positive rate is ≈ 2 · S · 2−F ,
≈ T −1 · (L − 1) + T −1  with Dostoevsky. The reason is that every            where F is the fingerprint size in bits. Querying entails at most two
K +1  Z +1
                                                                              memory I/Os as each entry is in one of two buckets.
compaction that an entry participates in leads to one BF insertion,

which costs one memory I/O. Table 1 summarizes these costs for

each of the merge policies. It shows that query cost over the BFs can         Promise. Cuckoo filter supports storing updatable auxiliary data
                                                                              for each entry alongside its fingerprint. We propose to replace
be significant, especially with tiering and lazy leveling. Moreover,

the BF’s query and construction costs both increase with respect              the LSM-tree’s multiple Bloom filters with a CF that maps each
to the number of levels L and thus with the data size. Finally, there         data entry not only to a fingerprint but also to a Level ID (LID),
is an inverse relationship between the BFs’ query and construction            mapping to the sub-level that contains the entry. The promise is to

costs: the greedier we set the LSM-tree’s merge policy to be (i.e., by        allow finding the run that contains a given entry with at most two
fine-tuning the the parameters T , K and Z ), query cost decreases as         memory I/Os, far more cheaply than with blocked Bloom filters.

3

<!-- page: 4 kind: pdf_page route: needs_review -->
| query k7 Cuckoo Filter LSM-tree update k1 (Z=1, K=2, T=3)
...
hash(k1)=X stnirpregnif level buffer 1 flush
h a s h ( k 3 ) = Y f a ls e k1 4 1 k12
h a s h ( k 4 ) = Q po s it iv e hash(k5)=W IDs 5 5 merge
k3 ... 3 empty ... 4
hash(k1)=X 2
k2, k4, k5, k6, k7 h h h a a a s s s h h h ( ( ( k k k 2 6 7 ) ) ) = = = O V Y 5 5 5 po tr s u it e i v e ... ... 5
update level IDs & remove duplicates ... | Term | Definition |  |
| --- | --- | --- | --- |
|  | B | CF bucket size (bits) |  |
|  | F | fingerprint size (bits) |  |
|  | D | level ID size (bits) |  |
|  | S | slots per CF bucket |  |
|  | pi | fraction of data at Level i |  |
|  | fj | probability of Level ID j |  |
|  | H | entropy of level ID probability distribution |  |
|  | Hcomb | entropy of combination probability distribution |  |
|  | C | the set of all level ID combinations |  |
|  | Cfreq | the set of most probable level ID combinations |  |

# 4 CHUCKY

# 4.1 Integration with LSM-Tree memory access cost is 1.5 I/Os on average to find the target entry

query k7  update k1                Cuckoo Filter                                LSM-tree (Z=1, K=2, T=3)  Term    Definition
                                                                                                             B    CF bucket size (bits)
                     fingerprints            ...  level IDs         buffer        1 k1  k1 2                 F    fingerprint size (bits)
                                                                     flush      empty     merge              D    level ID size (bits)
                                   hash(k1)=X 1                false                    k3 ... 4             S    slots per CF bucket
                                   hash(k3)=Y 4              positive                                        pi   fraction of data at Level i
                                   hash(k4)=Q 5                                                              fj   probability of Level ID j
                                   hash(k5)=W 5                              3                               H    entropy of level ID probability distribution
                                                                                                                  entropy of combination probability distribution
                                             ...              true     ... k2, k4, k5, k6, k7 ...     5   Hcomb   the set of all level ID combinations
                                                             positive                                        C    the set of most probable level ID combinations
                                   hash(k1)=X 2
                                   hash(k2)=O 5              update level IDs & remove duplicates          Cfreq
                                   hash(k6)=V 5
                                   hash(k7)=Y 5

...

The FPR, meanwhile, is given in Equation 5 by subtracting the LID                                        Querying. Chucky processes a query by accessing both CF buckets
size D from the memory budget M. The challenge is keeping D                                              that the key maps to (using Equation 4). For all matching finger-
small so that the FPR also stays small.                                                                  prints within these two buckets, it searches the corresponding runs
                                                                                                         from youngest to oldest. In Figure 3, for example, the application
          F P R ≈ 2 · S · 2−F = 2 · S · 2−M+D                                           (5)              queries for key k7. Chucky maps this key to two buckets, both of
                                                                                                         which have one entry with a matching fingerprint Y . One maps to
Case-Study. SlimDB [86] is the first system to replace an LSM-                                           Sub-Level 4 and one to Sub-Level 5. The query searches Sub-Level 4
tree’s Bloom filters by a Cuckoo filter with LIDs. It therefore pro-                                     first as it contains younger data, but it incurs a false positive. It
                                                                                                         then searches Sub-Level 5, where it successfully finds the target
vides an interesting case-study. SlimDB encodes LIDs as fixed-                                           entry. Since a query accesses two CF buckets, the overhead is two
length integers. Each LID therefore comprises at least log2(A) bits                                      memory I/Os, irrespective of the data size or merge policy.
to identify all runs uniquely, where A is the total number of sub-
levels from Equation 1. By plugging in log2(A) as D in Equation 5                                        Inserting New Data. Whenever the memtable gets flushed to stor-
and substituting for A, the FPR simplifies into Equation 6, which                                        age, Chucky adds a CF entry for each key in the batch (including for
indicates that the FPR increases with the number of levels L. The                                        tombstones). The overhead is approximately two memory I/Os per
reason is that the LIDs take away bits from the fingerprints as the                                      entry as entries may be swapped across buckets to clear space. For
                                                                                                         example, consider entry k1 in Figure 3, for which there is originally
data grows to be able to identify more sub-levels uniquely. Is it                                        one version at Sub-Level 2. A new version of this entry is then
                                                                                                         flushed into Sub-Level 1. Chucky adds a new mapping entry for
possible to keep the FPR lower and stable as the data size grows?                                        the new version while still keeping the older version’s mapping in
                                                                                                         the CF. This is in contrast to SlimDB, which would issue a storage
          F P Rbinary ≈ 2 · S · 2−M · (K · (L − 1) + Z )                                (6)              I/O to check if an entry with key k1 exists and if so update the
                                                                                                         existing mapping entry’s LID in the CF. Hence, Chucky removes
   Another issue with SlimDB is that it accesses storage before each                                     SlimDB’s read-before-write operation. The outcome is better per-
application write to check if the entry in question exists. If so, it                                    formance. The trade-off is that Chucky has to map obsolete entries
modifies the entry’s LID within the CF to reflect updated entry’s                                        in its CF until compaction, differently from SlimDB but similarly to
location. This entails a substantial overhead on the write path. Can                                     Bloom Filters, for which different versions of the same entry also
we keep the LIDs up-to-date without read-before-write operations?                                        take up space across multiple filters until compaction. A problem
                                                                                                         that can arise with Chucky is that CF buckets can overflow if too
4 CHUCKY                                                                                                 many obsolete versions of the same entry exist, as their mapping
                                                                                                         entries all get placed in the same pair of CF buckets. We handle
Chucky is a novel LSM-tree filter that simultaneously scales mem-                                        such overflows in Section 4.5 through extension buckets.
ory bandwidth, memory footprint, and the false positive rate (FPR).
It achieves this by replacing the Bloom filters by a Cuckoo filter                                       Maintenance. As an entry moves into a new sub-level during a
(CF) variant that uses level IDs to map each entry to its sub-level                                      compaction, Chucky updates its LID in the CF as it is brought into
within the LSM-tree. It further innovates along two directions.                                          memory to be merged. For every obsolete entry identified during
Opportunistic Maintenance. Chucky keeps the level IDs within                                             compaction, on the other hand, Chucky finds and removes the
the CF up-to-date opportunistically during merge operations at no                                        corresponding mapping entry from the CF. In Figure 3, for example,
additional storage I/O cost. We discuss this in Section 4.1.                                             compaction is triggered, merging the two runs at Sub-Levels 1 and 2
Level ID Compression. Chucky compresses level IDs to prevent                                             into one run at Sub-Level 3. During this operation, the older version
their size from increasing and stealing bits from the fingerprints as                                    of key k1 from Sub-Level 2 is removed from the CF. For the newer
the data grows. Thus, Chucky keeps the FPR low. We show how                                              version of k1, the LID is updated from 1 to 3 to reflect the new
to compress level IDs in Section 4.2. We identify and address the                                        sub-level. This approach does not involve any additional storage
implications of compressed level IDs on bucket alignment and com-                                        I/Os on top of the ones that are already issued for compaction. The
putational efficiency in Sections 4.3 and 4.4, respectively. Section                                     memory access cost is 1.5 I/Os on average to find the target entry
4.5 covers miscellaneous design considerations .                                                         across the two possible CF buckets that may contain it.

4.1 Integration with LSM-Tree                                                                               Interestingly, an entry’s LID does not need to be updated when
                                                                                                         the entry stays at the same sub-level after compaction. For example,
Figure 3 illustrates Chucky’s architecture. For every data entry in                                      suppose Sub-Levels 3 and 4 in Figure 3 now get merged with Sub-
the LSM-tree, there is one CF entry consisting of a fingerprint and                                      Level 5, and the resulting run stays at Sub-Level 5 as it does not
a level ID (LID) that maps the entry’s current sub-level number.
Figure 3 also illustrates Chucky’s query and update workflows with
solid blue arrows and dashed red arrows, respectively.

4

Figure 3: Chucky uses a Cuckoo filter to map entries in the LSM-tree, and it keeps this mapping up-to-date during compactions.

<!-- page: 5 kind: pdf_page route: static -->
# 4.2 Compressing Level IDs

Leveling Lazy-Leveling Tiering                                  Let us assume that all sub-levels of the LSM-tree are filled up

application query O(1)                  O (1)              O (1)              to capacity. Let us also assume that the average data entry size is
                                                                              the same in different runs. Under these two assumptions, Equa-
application update O(L)                 O (L)              O (L)              tion 8 gives the probability that a randomly selected LID from the
                                                                              Cuckoo filter corresponds to Sub-Level j. In other words, Equation 8
Table 2: Chucky’s memory I/O complexities.                                    becomes a probability distribution of the LIDs within the CF.

exceed its capacity. In this case, the LIDs of entries k2, k4, k5, k6 and        The assumption that all sub-levels are full reflects the case where
k7 stay the same. By contrast, with Bloom filters we would have to            memory pressure is highest. To optimize memory footprint for the
                                                                              worst-case, we maintain this assumption for the rest of Section 4.
rebuild the filter at Sub-Level 5 and reinsert each of these entries at       Entropy. Equation 9 derives the Shannon entropy of the LID prob-
                                                                              ability distribution, which represents the average number of bits
a significant memory I/O cost. Hence, a LID is updated at most L              needed to represent a LID after maximal compression. We derive it
times, once for each time it moves into a new level. The overall              by stating the definition of entropy on the left-hand side, plugging
overhead is therefore at most ≈ 1.5 · L amortized memory I/Os per             in Equations 8 for fj , taking the number of sub-levels A and thus
application insert/update/delete, irrespective of the merge policy.           the data size to infinity, and simplifying. Interestingly, the entropy
                                                                              converges with respect to the number of sub-levels A and hence
Memory I/O Complexities. Table 2 summarizes the memory I/O                    with the data size. The intuition is that the exponential decrease
complexities of Chucky. Relative to the BFs in Table 1, Chucky’s              in LID probabilities for smaller levels trumps the fact that LIDs at
                                                                              smaller levels would require more bits to represent uniquely.
core advantage is reducing query cost to a small constant that is

independent of the data size and of the merge policy. Chucky also

reduces the update cost complexities for leveling and lazy leveling,

thus eliminating the dependence of update cost on the merge policy.

In practice, for tiering and lazy leveling, Chucky’s update cost of           A                                            T
≈ 1.5 · L memory I/Os per entry may be slightly more expensive                                                       1 T T −1
than with blocked BFs. This, however, is counterbalanced by the                              T −1

substantial point read cost reduction.                                        H = A→∞ lim −fj · log2 fj = log2 Z T · K T · T − 1 (9)

Interplay with CPU Caching. For workloads with point skew,                    j =1
whereby the same data entries are repeatedly read by the appli-
                                                                              Chucky’s FPR Lower Bound. By plugging in the entropy H in
cation, Chucky can accommodate a larger working set within the                Equation 9 as the LID size D in Equation 5, we obtain an optimistic
                                                                              FPR approximation for Chucky in Equation 10. This is the lowest
CPU caches. The reason is that only two CF buckets need cached for

any frequently accessed entry. With blocked BFs, however, at most             we may expect the FPR to be by virtue of compressing each LIDs as
A filters need to be cached for such an entry. On the other hand,             much as possible and assigning all remaining bits to the fingerprints.
for workloads with areal skew, whereby entries in the same tempo-             We observe that this bound is asymptotically lower with respect
ral or spatial area are more likely to be read, BFs may better lend           to data size than the FPR for a CF with integer encoded LIDs in

themselves to CPU caching as they are more granular temporally                Eq. 6. It is also asymptotically lower than the FPR upper bound for

(across sub-levels) and spatially (across SSTs within a sub-level). In        uniformly allocated Bloom filters in Eq. 2. Finally, in comparison

terms of update cost, BFs for smaller runs of the LSM-tree may in             to the FPR upper bound with optimally allocated Bloom filters

practice fit in the CPU caches and thus entail fewer memory I/Os              in Equation 3, we observe that while Equation 10 has a higher
than predicted in Table 1. The effects of CPU caching are subtle.
While workloads favoring BFs may be envisioned, Chucky gives                  multiplicative constant of 2 · S, the FPR decreases more quickly
better worst-case guarantees and thus renders performance more                with respect to memory (i.e., ∝ 2−M as opposed to ∝ 2−M ·ln(2)).
robust across all cases and especially as the data grows.                     This implies that for a high enough memory budget (M ≥ 10 bits per

entry), Chucky should be able to beat state-of-the-art Bloom filters

in terms of FPR. This theoretical finding reaffirms our approach.

4.2 Compressing Level IDs                                                                                            T
                                                                                                   1 T T −1
This section establishes theoretical bounds on the compressibility                  −M T −1
of LIDs and explores their encoding design space in detail.                   F P Rchucky ≳ 2 · S · 2 · Z T · K T ·            (10)
                                                                                                                     T −1

LID Probability Distribution. Equation 7 denotes pi as the frac-              Huffman Coding. Chucky uses Huffman coding [46] to compress
tion of the LSM-tree’s overall capacity at Level i. The expression on         LIDs in practice. As input, the Huffman encoder takes the LIDs
the left is more accurate but quickly converges to the expression
                                                                              and their probability distribution (i.e., Eq. 8) for a particular LSM-
on the right as the number of levels grows. As expected, capacities           tree configuration (i.e., of T , K, Z and L). As output, it returns a
                                                                              code to represent each LID, where LIDs with a higher probability
of smaller levels are exponentially decreasing.

T − 1 T L−1                             T −1 1                                are assigned shorter codes. It does this by creating a binary tree
pi = L−i · L             lim pi =                 · L−i ,         (7)         from the LIDs by connecting the least probable LIDs first as sub-
                                                                              trees. A LID’s ultimate code length corresponds to its depth in the
T  T −1       L→∞                       TT                                    resulting tree. Figure 4 illustrates an example for an LSM-tree with
                                                                              labeled LIDs and their probabilities from Eq. 8. For example, LID
   Equation 8 denotes fj as the fraction of the LSM-tree’s capacity           6 contains a fraction of 5/124 ≈ 4% of the LSM-tree’s capacity, and
at Sub-Level j, which is a part of Level ⌈j/K⌉. Equation 8 is derived         therefore, when all sub-levels are full, ≈ 4% of all entries in the
                                                                              Cuckoo filter have a LID of 6. The Huffman encoder creates the tree
by dividing the level’s capacity p ⌈j/K ⌉ (from Eq. 7) by the number of       shown alongside for this LSM-tree instance. The code for a given
                                                                              LID is derived by concatenating the tree’s edge labels on the path
sub-levels A ⌈j/K ⌉ at that level (from Eq. 1). For example, in Figure 3
Sub-Level 5 is at Level ⌈5/2⌉ = 3, and so it comprises a fraction of

f3/A3 ≈ 0.62 of the overall LSM-tree’s capacity.

fj = (8) p⌈j/K ⌉
                    A⌈j/K ⌉

5

<!-- page: 6 kind: pdf_page route: static -->
| ode L | IDs’aver | agecod | elengtht | oconverge |
| --- | --- | --- | --- | --- |
| w | ithresp | ecttod | atasize. |  |
| fnode.Fo | rinstance | ,the | increase | the LSM-t |
| espectivel | y. |  | andthe | entropygro |
| odeisapr | efixofan | other | tendsto | wardszero. |

Level Freqs.                 LSM-tree            Huffman Tree

11                            1234                  0  1
                 124
                   5                        Z=1     0  19
                              5 6 7 8 K=4
   2 124                                         0  1  0  1
   3 100                                    T=5
                                    9            6 78     0  1
                 124
                                                       0  15

1 23 4                                              ZKT                       ZK

Figure 4: A Huffman tree encodes each                                                                                    L                                                       T
LID uniquely such that the average code
length is minimized.                                                  Figure 5: Compression causes the                      Figure 6: The average code length ap-
                                                                      LIDs’ average code length to converge                 proaches the entropy as larger permu-
                                                                      with respect to data size.                            tations of LIDs are used.

from the tree’s root to the given LID’s leaf node. For instance, the                              increase the LSM-tree’s size ratio T , the gap between the ACL
codes for LIDs 4 and 9 are 011011 and 1, respectively.                                            and the entropy grows; the ACL approaches one while the entropy

Decodability. With Huffman coding, no code is a prefix of another                                 tends towards zero. The reason is that a larger size ratio increases
code [46]. This property allows for unique decoding of an input bit                               the skew of the LID probability distribution by pushing a higher

stream by traversing the Huffman tree starting at the root until we                               proportion of the data to larger levels. With more skew, each LID

reach a leaf, outputting the LID at the given leaf, and then restarting                           carries less information, leading to a lower entropy and thus higher

at the root. For example, the input bit stream 11001 gets uniquely                                compressibility. However, each LID requires at least one bit to

decoded into LIDs 9, 9 and 7 based on the Huffman tree in Figure 4.                               represent with a code, and so the ACL cannot drop below one.

This property allows us to uniquely decode all LIDs within a bucket                               Hence, we cannot harness the increase in compressibility.

without the need for delimiting symbols.                                                          Level ID Permutations. As there are multiple LIDs at each CF
                                                                                                  bucket, we can push the compression barrier of one bit per LID
Average Code Length. We measure the encoded LIDs’ size using                                      by encoding multiple LIDs collectively. The Huffman Tree labeled
                                                                                                  Perms. Figure 7 gives a toy example of how to encode two LIDs
their average code length (ACL), defined as                     A     l  j  ·  fj,  where  lj     at a time as permutations, obtained by feeding every possible per-
                                                                j =1                              mutation of size S (two in this case) along with its probability (the
                                                                                                  product of the constituent LIDs’ probabilities) into a Huffman en-
is the code length assigned to LID j. For example, this equation

computes 1.52 bits for the Huffman tree in Figure 4. This is a saving

of 62% relative to integer encoding, which would require four bits

to represent each of the nine LIDs uniquely.

Memory Footprint Analysis. It is well-known in information                                        coder. As shown, the ACL now drops below one bit by virtue of
theory that an upper bound on a Huffman code’s ACL is the entropy                                 representing the most probable permutation with fewer bits than

plus one [46]. The intuition for adding one is that each code length                              the number of LIDs within it. Interestingly, Figure 6 shows that

must be rounded up to an integer number of bits. We express this                                  as we increase the number of collectively encoded LIDs within a
as ACL ≤ H + 1, where H is the entropy from Eq. 9. We therefore                                   permutation, the ACL approaches the entropy.
expect the ACL to also converge and become independent of the
                                                                                                  Level ID Combinations. To push compression even further, we
data size, similarly to Eq. 9. We verify this in Figure 5 by increasing                           can encode combinations as opposed to permutations of LIDs. A

the number of levels for the example in Figure 4 and plotting the                                 combination, unlike a permutation, disregards information about

Huffman ACL, which indeed converges (in contrast to integer-                                      the ordering of entries. As there are fewer possible combinations

encoded LIDs). The reason is that while runs at smaller levels are                                than  permutations  of    LIDs  within  a  CF  bucket  (  S +A−1  as opposed
                                                                                                                                                               S
assigned longer codes, they are exponentially less probable, so the                               to AS ), we need fewer bits on average to represent them.

smaller codes of runs at larger levels dominate the ACL.

The probability distribution of LID combinations is multinomial.

Tight ACL Upper Bound. Huffman coding is known to be optimal                                      For n independent trials, each leading to a success for one of k
in that it minimizes the ACL [46]. However, the precise ACL is                                    categories, with each category having a fixed success probability,

difficult to analyze because the Huffman tree structure is difficult                              the multinomial distribution gives the probability of any particular

to predict from the onset. Instead, we can derive an even tighter                                 combination of successes across the various categories. In our case,
upper bound on the ACL than H + 1 by assuming a less generic
coding method and observing that the Huffman ACL will be at                                       the number of trials is the number of slots S per CF bucket, the
                                                                                                  different categories are the A LIDs, and the success probabilities
least as short. Let us represent each LID using (1) a unary encoded
                                                                                                  are given by the LID probability distribution in Equation 8.
prefix of length L − i + 1 bits to represent Level i followed by (2) a
truncated binary encoding suffix of length ≈ log2(Ai ) to represent                                  Now, let us denote c(j) as the number of occurrences of LID j
each of the Ai sub-levels at Level i uniquely. This is effectively a                              within a combination c. Equation 12 gives cprob as the probability
Golomb encoding [43]. We derive this encoding’s average length in                                 of combination c using the multinomial distribution. By feeding all

combinations and their probabilities into a Huffman encoder for

Equation 11 as ACLU B and illustrate it in Figure 5 as a reasonably                               the example in Figure 7, we obtain the Huffman tree titled Combs,
tight upper bound of the Huffman ACL.
                                                                                                  where the combination 12 replaces the two prior permutations 12
                       L T 1 T −1
AC LU B = lim                pi · L − i + 1 + log2 (Ai ) = T − 1 + log2(K T · Z T ) (11)          and 21. For this combination, we have S = 2, c(1) = 1 and c(2) = 1,
                       i =1                                                                       and so its probability is 2! · (1/11) · (10/11) = 20/121.
                  L→∞

Proximity to Entropy. Figure 5 also plots the entropy of the LID                                                                             A fjc (j)
                                                                                                                            cpr ob = S ! · c(j)! (12)
probability distribution from Eq. 9. As shown, there is a gap between
                                                                                                                                                 j =1
the Huffman ACL and the entropy. Figure 6 shows that as we

6

<!-- page: 7 kind: pdf_page route: static -->
# 4.3 Aligning Level ID Codes with Fingerprints

pi LSM-tree Single    Perms.        Combs.                                                      avg. fingerprint size (bits)                                          theoretical maximum
      Z=1, K=1, T=10
                                                 1                                                                                                                               MF & Fluid
1           1                    1         1 22                                                                                     FingerMparilnletainbgle(MF)          Alignment Coding
11 1  12                            12 11
10 2  0                    0        0                                                                                                                                                   (FAC)
11                                       0
                      0    1 22                                                                                                                  finguniform
                                                                                                                                                     erprin
                      21   0     1                                                                                            ZZ=1, KK=1, TT=10             ts

11 12                               ZKT L                                                          LL=6, SS=4, BB=32

ACL:     1                 0.63     0.58            Figure 8: Encoding level IDs as large                      fraction of buckets that overflow (1-NOV)
                                                    combinations maximizes compress-
Figure 7: Encoding level IDs as permu-              ibility.                                    Figure 9: Chucky resolves the con-
tations or combinations allows reducing                                                         tention between fingerprint size and
the average code length below one.                                                              bucket overflows via FM and FAC.

Combinations Analysis. The fact that combinations exclude in-                 Figure 10 Part (A) implies that there is a contention between the
formation about ordering causes a reduction in entropy. Equation 13        propensity of buckets to overflow and the fingerprint size. While
                                                                           decreasing the fingerprint size alleviates overflows in some buckets,
derives Hcomb as the entropy of the LID combinations distribution          it results in a higher false positive rate for the filter as a whole. We
by using the multinomial distribution’s entropy function and plug-         substantiate this contention in Figure 9 with the curve labeled uni-
                                                                           form fingerprints. The x-axis measures the fraction of overflowing
ging in S, A, and Eq. 9. Figure 8 shows that as we increase the            CF buckets while the y-axis measures the fingerprint size. Is it pos-
combination size, Hcomb drops relative to H as it eliminates more
ordering information. This leads to an increase in compressibility.

1 A S S j S−j                                                        sible to eliminate this contention so as to guarantee few overflows

Hcomb = H − · log2(S !) −           · fi · (1 − fi ) · log2(j!) (13)       and large fingerprints at the same time?
      S i=1 j=0 j
                                                                           Malleable Fingerprinting (MF). To enable better alignment of
The ACL with combinations is c ∈C (lc ·cprob )/S where C is the set        codes and fingerprints, we introduce MF. The goal is to counterbal-
of all combinations and lc is the code length for Combination c (we
divide by S to express the ACL per LID rather than per bucket). We         ance the fact that entries from larger levels tend to have smaller

combination code lengths and to use the spare space in the bucket

observe that the combinations ACL is strictly lower than the permu-

for having longer fingerprints. Thus, MF assigns entries at smaller

tations ACL in Figure 8, and that it converges with the combinations

levels of the LSM-tree shorter fingerprints. As they get merged into

entropy as we increase the number of collectively encoded LIDs. In

larger levels, however, they get assigned longer fingerprints.

the rest of the paper, we continue with encoded combinations as

The question with MF is how to choose a fingerprint length for

they achieve the best compression.

each level so as to carefully control the balance between fingerprint

Bucket Structure. Each CF bucket in Chucky commences with one              lengths and bucket overflows. We frame this as an constrained op-
combination code followed by S fingerprints. Since the combination
code excludes information about ordering, the fingerprints within          timization problem, where the objective is to maximize the average

fingerprint length,  L                             F Pi  · pi ,  and  where          F Pi  be    an  integer         de-
                                                                                                i =1
the bucket are sorted based on their LIDs in order to be able to infer
                                                                           noting the length of fingerprints of entries at Level i. The problem
which fingerprint corresponds to which LID.
                                                                           is defined for 2B >  S +A−1                                                                      B
                                                                                                   S                          ,     meaning      the  bucket          size      has    to       be

4.3 Aligning Level ID Codes with Fingerprints                              at least large enough to identify all combinations uniquely. We

Since LIDs are variable-length due to compression, aligning them           constrain the problem using a parameter NOV for the fraction of
along with fingerprints within CF buckets becomes a challenge. We
depict this challenge in Figure 10 Part (A) with sixteen-bit CF buck-      non-overflowing buckets we wish to guarantee (ideally at least
ets that need to store one combination code for two entries along          0.9999). We use this parameter to define Cf r eq as a subset of C
with two five-bit fingerprints (FPs). This example is based on the         that contains only the most probable LID combinations in C such
LSM-tree instance in Figure 4 except we now encode combinations            that their cumulative probabilities fall just above NOV 1. We define
rather than every LID individually. The term lx,y in the figure is the     Equation 14 as a constraint requiring that for all c ∈ Cf r eq , the code
code length assigned to a bucket with LIDs x and y. We observe that        length (denoted lc ) plus the cumulative fingerprint length (denoted
some codes and fingerprints perfectly align within a bucket (Row I).       cF P ) do not exceed the number of bits B in the bucket2.

∀c ∈ Cf r eq : cF P + lc ≤ B                                                                                (14)

However, others exhibit underflows (Row II), meaning some bits at             While optimization problems involving integers are known to be
the end of the bucket are unused. Still other buckets exhibit over-        difficult to solve, we exploit the particular structure of our problem
flows (Rows III and IV), meaning the cumulative length of the code         with an effective hill-climbing approach shown in Algorithm 1. The
and the fingerprints exceeds the bucket’s size. Underflows occur at        algorithm initializes all fingerprint lengths to zero. It then increases
buckets with more probable LIDs (belonging to larger levels) as a          larger levels’ fingerprint sizes as much as possible, moving to a next
result of having shorter combination codes. They are undesirable           smaller level if the overflow constraint in Equation 14 is violated.
as they waste bits that could have otherwise been used for having

larger fingerprints. On the other hand, overflows occur in buckets         1Formally, Cf r eq is defined such that minc ∈Cf r eq cpr ob ≥ maxc Cf r eq cpr ob and
with less probable LIDs (belonging to smaller levels) as a result
of having longer combination codes. They are undesirable as they           N OV ≤ c ∈Cf r eq cpr ob ≤ N OV + minc ∈Cf r eq cpr ob .
require storing the rest of the bucket contents elsewhere. This can
result in poorer memory utilization and higher access costs.               2More concretely, for a combination c let c(j) denote the number of occurrences of

the jth LID. Then c ’s cumulative fingerprint length is cF P =                             A     F P⌈j/K ⌉  · c(j).  The
                                                                                                                                                                      j =1

term F P⌈j/K ⌉ is the fingerprint size set to the jth LID, which is at Level ⌈j/K ⌉.

7

<!-- page: 8 kind: pdf_page route: static -->
(A) Uniform Fingerprints                                    (B) Malleable Fingerprinting (MF)                         (C) MF & Fluid Alignment Coding

l1,9          FP              FP                                   l1,9      FP1             FP3                              l1,9             FP1     FP3

I           6 bits        5 bits          5 bits                               6 bits    3 bits          7 bits                           5 bits          4 bits   7 bits

l9,9  FP             FP             underflow                     l9,9   FP3               FP3     underflow                 l9,9           FP3              FP3

II 1 bit 5 bits           5 bits               5 bits                   1 bit  7 bits            7 bits       1 bit               2 bits          7 bits           7 bits

l5,5                  FP              FP                           l5,5              FP2      FP2                             l5,5            FP2      FP2

III         8 bit                 5 bits          5 bits overflow              8 bit             4 bits  4 bits                           6 bits          5 bits   5 bits

l1,5                  FP           FP                         l1,5           FP1             FP2                       l1,5             FP1    FP2
                                                                                                                 4 bits overflow
IV                10 bit                  5 bits       5 bits overflow           10 bit          3 bits                                   7 bits           4 bits  5 bits

Figure 10: Storing compressed level ID codes with uniformly sized fingerprints leads to poor bucket alignment (Part A). We
solve this problem using Malleable Fingerprinting (Part B) and Fluid Alignment Coding (Part C).

1 for i ← 1 to L by 1 do F Pi ← F Pmin end                                                  assigned within a Huffman code, the longer other codes must be for
                                                                                            all codes to remain uniquely decodable. This trade-off is embodied
2 F Pmax = M − 1                                                                            in the Kraft-McMillan inequality [64, 75], which states that for a
                                                                                            given set of code lengths L, all codes can be uniquely decodable
3 for i ← L to 1 by −1 do                                                                   if 1 ≥ l ∈L 2−l . The intuition is that code lengths are set from a
                                                                                            budget amounting to 1, and that smaller codes consume a higher
4 for b ← F Pmin + 1 to F Pmax by 1 do                                                      proportion of this budget.

5           temp ← F Pi                                                                        To exploit this trade-off, FAC assigns longer codes to occupy
                                                                                            the underflowing bits for the most probable bucket combinations.
6           FPi ← b                                                                         As a result, the codes for less probable bucket combinations can
                                                                                            be made shorter. This creates more space in less probable buckets,
7           if overflow constraint is violated then                                         which is exploited to reduce overflows and to increase fingerprint
                                                                                            sizes for smaller levels. Figure 10 Part (C) illustrates this idea. The
   8                F Pi ← temp                                                             combination in Row II, which is the most common in the system,
                                                                                            is now assigned a longer code by one bit to remove the underflow.
   9                F Pmax = temp                                                           This allows reducing the code lengths for all other combinations,
                                                                                            which in turn allows setting longer fingerprints to entries at Levels 1
   Algorithm 1: Maximizing the average fingerprint size by                                  and 2 as well as to eliminate the bucket overflow in Row IV.

hill-climbing.                                                                              We implement FAC on top of MF as follows. First, we replace the
                                                                                            previous constraint in Equation 14 by a new constraint, given in
The rationale for lengthening larger levels’ fingerprints first is that                     Equation 15. Expressed in terms of the Kraft-McMillan inequality,
                                                                                            it ensures that the fingerprint sizes stay short enough such that it
their entries are more common in the CF. Hence, the algorithm                               is still possible to construct non-overflowing buckets with uniquely
                                                                                            decodable codes for all combinations in Cf r eq . It also ensures that
follows the steepest ascent with respect to maximizing the objective                        all bucket combinations not in Cf r eq can be uniquely identified
                                                                                            using unique codes that are at most the size of a bucket B.
function. Figure 10 Part (B) gives an example of how MF allows for
                                                                                                                            2−(B−cF P ), for c ∈ Cf r eq
entries from larger levels to have longer fingerprints (Row II) while                                            c ∈C 2 1 ≥ −B, else (15)

at the same time eliminating some overflows (Row III). The result                                                           

is a better balance between overflows and average fingerprint size                             Equation 15 does not rely on knowing Huffman codes in advance
                                                                                            (i.e., as Equation 14 does). Thus, we run the Huffman encoder after
as shown in Figure 9.                                                                       rather than before finding the fingerprint lengths using Algorithm 1.
                                                                                            Furthermore, we run the Huffman encoder only on combinations
      Since MF assigns different fingerprint lengths to different ver-                      in Cf r eq while setting the probability input for a combination c
                                                                                            as 2−(B−cF P ) as opposed to using its multinomial probability (in
sions of the same entry across different levels, a problem arises                           Equation 12) as before. This causes the Huffman encoder to gen-
                                                                                            erate codes that exactly fill up the leftover bits B − cF P . For all
whereby the Cuckoo filter can map these different versions of the                           combinations not in Cf r eq , we set uniformly sized binary codes of
                                                                                            size B bits, which consist of a common prefix in the Huffman tree
same entry to more than two CF buckets. The reason is that Equa-                            and a unique suffix. Hence, we can decode both sets uniquely.

tion 4 relies on an entry’s fingerprint to compute the alternative

bucket location, and so different fingerprint lengths would lead

to different bucket addresses. We resolve this by ensuring that all

fingerprints comprise at least F Pmin bits, and we adapt the CF to
determine an entry’s alternative bucket based on its first F Pmin
bits. This forces all versions of the same entry to reside in the same

pair of CF buckets. While this constraint slightly reduces the av-

erage fingerprint size given by Algorithm 1, it provides a lower

FPR variance as no entries are assigned very small fingerprints. In
accordance with the original CF paper [39], we set F Pmin to five
bits to ensure that an entry’s two buckets are independent enough

to achieve a 95% space utilization.

Fluid Alignment Coding (FAC). Figure 10 Part (B) illustrates that
even with MF, underflows and overflows still occur (Rows II and
IV, respectively). To further mitigate them, we introduce FAC. FAC
exploits a well-known trade-off that the smaller some codes are

8

<!-- page: 9 kind: pdf_page route: static -->
# ing the underflowing bits of the most probable combination codes, 4.4 Optimizing Decoding & Recoding

ZKT                                                                  ZKT               ZKT
                                                              LSB                                                                  LSB               L SB

Figure 11: The FPR decreases expo-                                                                                        L             Figure 13: The decoding table access
nentially as newer entries are ac-                                                                                                      cost increases slowly with data size
cessed by queries.                                                    Figure 12: The Huffman tree size con-                             until flattening at one memory I/O.
                                                                      verges while the de/recoding table
                                                                      sizes grow slowly with data size.

The horizontal curve in Figure 9 shows that MF and FAC elimi-                                than where the target entry resides. The figure demonstrates that
                                                                                             Equation 16 predicts within reason the case where the target entry
nate the contention between overflows and fingerprint size when                              does not exist, and that it provides a reliable upper bound in all
                                                                                             cases where the entry does exist.
applied together; fingerprints stay long and overflows stay rare

at the same time. The trade-off is that the average code length

becomes slightly longer than before. The reason is that by occupy-

ing the underflowing bits of the most probable combination codes,                            4.4 Optimizing Decoding & Recoding
FAC makes the ACL at least S bits long (≥ 1 bit per entry). This
means that achieving good bucket alignment requires sacrificing                              We now discuss how to efficiently decode and recode combinations

codes during application reads and writes.

some space. Figure 9 measures this sacrifice as the gap between the                          Cached Huffman Tree. A Huffman code is typically decoded one
                                                                                             bit at a time by traversing the Huffman tree from the root to a
curve for MF & FAC and the curve labeled theoretical maximum,

obtained by subtracting the entropy (from Eq. 13) from the memory                            leaf node. A possible problem is that traversing it can require as
budget M. It stands as ≈ 1/2 bit per entry for our example, a modest
price. We use MF and FAC by default for the rest of the paper.                               much as one memory I/O per node visited. This cost grows with the

data size as the Huffman tree becomes deeper when there are more

Construction Time. The run-time complexity of Algorithm 1 is                                 levels. To restrict this cost, we observe that the bucket combination
O((L + M − Mmin ) · |C |), where L + M − Mmin is the number
of iterations and |C | is the cost of evaluating the constraint in                           distribution in Equation 12 is heavy-tailed. Hence, it is feasible to

keep a small Huffman Tree in the CPU caches to allow to quickly

Equation 15. In addition, the time complexity of the Huffman en-                             decode only the most common combination codes. Hence, we only

coder is O(|Cf r eq | · log2(|Cf r eq |)). To express these bounds more                      construct a Huffman tree for the most common LID combinations

loosely but in closed form, note that |Cf r eq | ≤ |C | =                   A+S −1  <        within the set Cf r eq , and we set the parameter NOV to 0.9999
                                                                               S             so that the set Cf r eq comprises 99.99% of the most common com-
        − 1)S  · ( e )S     AS . This workflow is seldom invoked, only                       binations within the CF. Figure 12 shows that the corresponding
(A + S                   <
                  S

when number of LSM-tree levels changes, and it can be performed

offline. Its run-time is therefore practical. Each of the points in                          Huffman tree’s size converges with respect to the data size. The

False Positive Rate (FPR). Chucky’s FPR is tricky to precisely                               Eq. 12) is convergent with respect to the number of levels, and so
analyze because the fingerprints have variable sizes that are not
                                                                                             any set whose size is defined in terms of its constituent members’

known in advance. Instead, we conservatively approximate the FPR                             cumulative probabilities is also convergent in size with respect to

to still allow reasoning about system behavior. We use the ACL                               the number of levels. This property ensures that the Huffman tree

upper bound ACLU B from Equation 11 to slightly overestimate the                             does not exceed the CPU cache size as the data grows.
average combination code length per entry with FAC. By plugging
                                                                                             Decoding Table (DT). In addition to the Huffman tree, there is a
in ACLU B for D in Equation 5, we obtain Equation 16, for which                              Decoding Table in main memory to allow decoding combinations
the interpretation is the expected number of false positives for a
                                                                                             codes not in Cf r eq . To ensure fast decoding speed for DT, we exploit
query to a non-existing key.                                                                 the property from in the last subsection that all bucket combinations

−M  T                                1  T −1                   not in Cf r eq have uniformly sized codes. Hence, we structure DT as
        F P Rchucky ≈ 2 · S · 2 · 2 T −1 · K T · Z T                                         an array whereby index i contains the LIDs that code i corresponds
                                                                                    (16)     to. This guarantees decoding speed in one memory I/O. Figure 12

Figure 11 compares Equation 16 to Chucky’s actual FPR. The                                measures the DT size as we increase the number of levels on the x-
x-axis varies the level whereon the target entry resides, where 6 is
the ID of the largest level and ‘none’ means the target entry does                           axis (each DT entry is eight bytes). As DT contains ≈ |C | =  A+S −1
not exist. The y-axis measures the average number of false positives                                                                                          S
incurred per query. The FPR drops exponentially when the target
entry is at smaller levels. The reason is that a point read accesses                         entries, its size grows slowly with the data size and stays smaller
the relevant levels from smallest to largest (to be able to find the
most recent version of the entry), and it terminates once it finds the                       than 1MB even for a large LSM-tree instance with ten levels.
first matching entry. As a result, exponentially fewer false positives
take place as there are, on average, exponentially fewer entries                             When point queries target data at smaller levels, the DT is more
within the target two buckets that correspond to even smaller levels
                                                                                             likely to be accessed. Figure 13 varies the level whereon the target

entry resides and measures on the log-scale y-axis the average

number DT accesses per query. The reason for the increase in

access cost for entries at smaller levels is that a bucket that has at

least one LID corresponding to a smaller level is less likely to be in

9

Figure 9 takes a fraction of a second to generate.                                           reason is that the probability of a given bucket combination (in

<!-- page: 10 kind: pdf_page route: static -->
# 4.5 Additional Design Considerations 5 EVALUATION

the set Cf r eq and hence in the cached Huffman tree. However, this               full CF bucket encountered during a query or update thus adding
overhead eventually flattens and therefore stays modest even in the               to them at most O(1) additional memory access.
worst-case. Overall, compared to a plain Cuckoo, which accesses on                Persistence. For each run, Chucky persists the fingerprints of all
average 1.5 buckets before finding a matching fingerprint, Chucky                 entries in storage. During recovery, it reads only the fingerprints
always accesses two buckets and potentially also the decoding table,              from storage and thus avoids a full scan over the data. It inserts each
leading to three memory I/Os.                                                     fingerprint along with its LID into a brand new CF at a practically
Overflow Hash Table. To handle bucket overflows, we use a small                   constant amortized memory I/O cost per entry. In this way, recovery
hash table to map from an overflowing bucket’s ID to the corre-                   is efficient in terms of both storage and memory I/Os.
sponding fingerprints. Its size is ≈ (1 − NOV ) = 10−4 of the CF size.            Range Reads. Similarly to mainstream KV-stores [3, 4, 38], Chucky
It is accessed seldom, i.e., only for infrequent bucket combinations,             processes a range read by accessing the relevant key range at each
and it supports access in O(1) memory I/O.                                        run without using the cuckoo filter. Range reads are therefore not
Recoding Table (RT). To find the correct code for a given com-                    directly affected by this work. Note, however, that applications
bination of LIDs while handling application writes, we employ a                   with many range reads often opt for a leveled LSM-tree, whereon
Recoding Table, implemented as a fast static hash table. It costs                 Bloom filters constitute high construction overheads. Chucky can
at most O(1) memory I/O to access and its size scales the same as                 indirectly improve performance for such applications by improving
the Decoding Table in Figure 12. Note that the most frequent RT                   write throughput and thus system performance as a whole.
entries are in the CPU caches during run-time and thus cost no                    Batch Updates. Chucky can support batch updates by (1) atomi-
memory I/Os to access.                                                            cally inserting a batch into the WAL and the memtable, (2) inserting
Space Summary. Figure 12 illustrates the CF size as we increase                   all entries in the batch into the CF, (3) asynchronously flushing
the number of LSM-tree levels. All auxiliary data structures are                  the memtable to storage when it is full, and finally (4) atomically
comparatively small and therefore not space bottlenecks.                          removing the memtable from the read path.

4.5 Additional Design Considerations                                              5 EVALUATION

This section discusses additional design considerations.                          We now show experimentally that Chucky renders memory and

Sizing & Resizing. When Chucky reaches capacity, it needs to                      storage bandwidth more robust than with existing designs.
be resized to accommodate new data. Since a CF needs to access                    Baselines. We use our own LSM-tree implementation, designed
the base data in order to be resized, we exploit the fact that merge              based on Dostoevsky [28]. We added as baselines blocked [84]

operations into the largest level of the LSM-tree pass over the                   and non-blocked BFs with uniform false positive rates (FPRs) to

entire dataset. We use this opportunity to also build a new and                   represent design decisions in RocksDB [38] and Cassandra [3],

larger instance of Chucky.                                                        respectively. We also support optimal FPRs [25]. We implemented

Partitioning. Since Cuckoo filter relies on the xor operator to                   Chucky as described in Section 4. We support a version of Chucky
locate an entry’s alternative bucket, the number of buckets must be               with uncompressed level IDs to loosely represent SlimDB [86].

a power of two. This can waste up to 50% of the allotted memory,                  Setup. The default setup consists of a Lazy-Leveled LSM-tree with
especially whenever LSM-tree’s capacity just crosses a power of                   a 1MB buffer, a size ratio of five, and with six levels amounting
two. To keep memory better-utilized, Vacuum filter [96] proposes                  to ≈ 16GB of data. Each entry is 64B. There is a 1GB block cache,

partitioning a CF into multiple independent CFs, each of which is                 and the database block size is 4KB. Chucky uses ten bits per entry

a power of two, but where the overall number of CFs is flexible.                  and 5% over-provisioned space. Hence, all BF baselines are assigned
Each key is mapped to one of the constituent CFs using a hash                     a factor of 1/0.95 more memory to equalize memory across the
modulo operation (similarly to blocked Bloom filters). In this way,               baselines. Every point in the figures is an average of three experi-

capacity becomes adjustable by varying the number of CFs. While                   mental trials. We use a uniform workload distribution to represent

Chucky does not support this yet, it is an important future step for              worst-case performance and a Zipfian distribution to create skew

memory-sensitive applications.                                                    and illuminate performance properties when the most frequently

Empty CF Slots. We represent empty fingerprint slots using a                      accessed data is in the block cache. To account for filter resizing
reserved all-zero fingerprint coupled with the most frequent LID                  overheads, any experiment that measures write cost commences

to minimize the corresponding combination code length.                            with an LSM-tree state whereby all levels but the largest are empty.

Entry Overflows. Since a CF maps multiple versions of the same                    We then fill them up with writes until a major compaction into
entry from different LSM-tree runs into the same pair of CF buckets,              the largest level occurs, leading to filter resizing. In Figure 14 Parts
a bucket overflow can take place if there are more than 2 ·S versions             (A) to (D), we evaluate filter performance in isolation from other
of a given entry. Some filters address this problem using embedded                parts of the system (e.g., memtable, storage I/Os, block cache, block
fingerprint counters (e.g., Counting Quotient Filter [81]). Chucky,               index). We focus on end-to-end performance in Parts (E) to (H).
however, uses an additional hash table (AHT), which maps from                     Platform. Our machine has 32GB DDR memory, Xeon E3-1505M
bucket IDs to the overflowing entries. With insertion-heavy work-                 v5 with four 2.8 GHz cores and 8MB L3 caches. It runs Ubuntu 18.04

loads, AHT stays empty. Even with update-heavy workloads, AHT                     LTS and is connected to a 750GB Intel Optane SSD DC P4800X.

stays small since LSM-tree by design limits space-amplification                   Memory I/O Scalability. Figure 14 Part (A) compares read/write

and thus the average number of versions per entry (e.g., at most                  latency with Chucky against blocked and non-blocked BFs (both
                                                                                  with optimal FPRs) with a uniform workload as the data grows.
 T    ≤  2  with  Leveling  or  Lazy  Leveling).  We  check  AHT  for  every
T −1

<!-- page: 11 kind: pdf_page route: static -->
9                                        avg. false positives per lookup  0.5                                        avg. false positives per lookup                                                              avg. false positives per lookup  0.25
                   8 non-blocked BFs blocked BFs
                                                                                             0.45  Chucky uncomp.                                                        1                     uniform BFs                                                                               Chucky uncomp.

0.4 uniform BFs optimal BFs                                                                       Chucky uncomp.                                                         0.2 uniform BFs
                                                                                                                                                                                                                                                                                                    optimal BFs
avg. latency (µs)  7  Chucky

6  writes                                                                 0.35  Chucky model                                                                                                                                                                          Chucky model

5 reads                                                                   0.3   Chucky                                                                10 1                                                                                         0.15               Chucky

4                                                                         0.25

0.2                                                                                                                                                                      0.1

3                                                                         0.15                                                                        10 2
                                                                                                                                                                                         optimal BFs
                   2                                                                         0.1                                                                                         Chucky                                                                       0.05

1                                                                         0.05                                                                        10 3 Chucky model
                                                                                                                                                                               4 6 8 10 12 14 16
                   0                                                                         0.0                                                                                                                                                                      0.0
                     23456789                                                                    23456789                                                                         (C) ﬁlter memory (bits/entry)                                                                Tiering Lazy-Leveling Leveling
                              (A) data size (levels)                                                                                                                                                                                                                              (D) LSM-tree variants
                                                                                                   (B) data size (levels)

18 optimal                     ﬁlter                                      18                           ﬁlter                                          102      non-blocked BFs                                    450000
                   16 blocked                     memtable                                                                memtable
avg. read latency (µs)                            fences    avg. read latency (µs)           16                           fences                                                  blocked BFs                                        400000
                  Chuck   BFs                     storage                     Chuck                                       storage
                        y14                                                         y        14                                         avg. write latency                        Chucky                         throughput (ops/s)  350000
                   12
                   10                                                                        12                                                                                   no ﬁlters                                          300000
                                                                                                  optimal
                                                                                                                                                                         10                                                          250000
                                                                                             10 blocked
                   8                                                                          8 BFs                                                                                                                                  200000

6                                                                         6                                                                                                                                       150000                                 Chucky

4                                                                         4                                                                                                                                       100000                                 Chucky uncomp.

2                                                                         2                                                                                                                                       50000                                  blocked BFs

0                                                                         0                                                                           1                                                                                            0     non-blocked BFs
                         Tiering Lazy-Leveling Leveling                                            Tiering Lazy-Leveling Leveling
                                                                                                                                                                               2  4  6         8   10 12                                                                    2345678

(E) target keys in storage                                                   (F) target keys in block cache                                                 (G) size ratio with leveling                                                              (H) data size (levels)

Figure 14: Chucky scales memory I/Os with data size (A) and for any LSM-tree variant (B). At the same time, Chucky’s false
positive rate scales with data sizes (C) with memory footprint (D), and for any LSM-tree variant (E). Hence, it improves read
latency when target data is in fast storage (F) or cached (G), resulting in more scalable throughput (H).

Write latency is measured by dividing the overall time spent on                                                                                                          scales the FPR better with respect to memory. Part (D) show that
filter maintenance by the number of writes issued by the application.                                                                                                    these results hold for any LSM-tree variant. Overall, Parts (B) to (D)
Read latency is measured just before a full merge operation (when                                                                                                        show that Chucky is at least on par with optimal BFs with respect
there are the most runs in the system) to highlight worst-case                                                                                                           to scaling the FPR vs. memory budget trade-off.
performance. Non-blocked BFs exhibit the fastest growing latency
as they require multiple memory I/Os per filter across a growing                                                                                                         Data in Storage vs. Memory. Figure 14 Parts (E) and (F) measure
number of filters. With blocked BFs, read/write latency grows more                                                                                                       end-to-end read latency with uniform and Zipfian (with parameter
slowly as they require at most one memory I/O per read or write.                                                                                                         s = 1) workloads, respectively. Read latency is broken in four
Chucky’s write latency also grows slowly with data as there are                                                                                                          components, including storage I/Os, fence pointers, memtable, and
more levels across which to update LIDs. Crucially, Chucky is the                                                                                                        filter search. In Part (F), relevant data is most often in storage and so
only baseline that keeps read latency low with data size as each                                                                                                         storage I/Os dominates read cost. Since our SSD is fast, however, the
read requires a constant number of memory I/Os.                                                                                                                          BFs probes still impose a significant latency overhead that Chucky
                                                                                                                                                                         is able to eliminate. In Part (F), on the other hand, the workload is
FPR Scalability. Figure 14 Part (B) compares the FPR for Chucky                                                                                                          skewed, meaning that target data is most often in the block cache.
with both compressed and uncompressed LIDs to blocked BFs with                                                                                                           In this case, the BFs become a bottleneck as they must be searched
both uniform and optimal space allocation. As we increase the                                                                                                            before the relevant block in the cache can be identified. Chucky
data size, the FPR of Chucky with uncompressed LIDs increases                                                                                                            alleviates this bottleneck thus significantly improving read latency.
since the LIDs grow and steal bits from the fingerprints. With
uniform BFs, the FPR also grows with data size as there are more                                                                                                         End-to-End Write Cost. Figure 14 Part (G) highlight’s Chucky’s
filters across which false positives can take place. In contrast, with                                                                                                   ability to keep filter construction overheads low as we increase
optimal BFs, smaller levels are assigned exponentially lower FPRs,                                                                                                       merge greediness (e.g., to optimize for range reads). We start with
and so the sum of FPRs converges to a constant that’s independent                                                                                                        a leveled LSM-tree with size ratio two on the left-hand side and
of the number of levels. Similarly, Chucky’s FPR stays constant as                                                                                                       increase it along the x-axis. The y-axis measures end-to-end write
the data grows since the average LID code length converges, thus                                                                                                         cost, derived by dividing the overall time spent processing these
allowing most fingerprints to stay large. The figure also includes                                                                                                       updates by the number of updates issued by the application. As we
the FPR model of Chucky from Equation 16 to show that, here too,                                                                                                         increase the size ratio, write cost across all baselines increases since
it gives a reasonable approximation of the FPR in practice.                                                                                                              there is more overlap between runs at adjacent levels, and so more
                                                                                                                                                                         data needs to be rewritten on average during each merge operation.
   Figure 14 Part (C) shows that Chucky requires at least eight bits                                                                                                     With relatively lower merge greediness (i.e., to the left of the figure),
per entry to work (i.e., for codes and minimum fingerprint sizes).                                                                                                       Chucky and blocked Bloom filters have similar construction over-
However, with eleven bits per entry and above Chucky offers better                                                                                                       heads. However, as we increase the size ratio, end-to-end write costs
memory/FPR trade-offs than all BF variants. The reason is that                                                                                                           with blocked Bloom filters increase more rapidly. The reason is that
BFs are known to exhibit suboptimal space use, which effectively                                                                                                         Bloom filters must be constructed from scratch during each merge
reduces the memory budget by a factor of ln(2). Thus, Chucky                                                                                                             operation, and so their construction overheads are proportional

<!-- page: 12 kind: pdf_page route: static -->
# 6 RELATED WORK vectoriziation [83], deletes for some but not all entries [88], efficient

# 86, 98, 99], which result in more runs and thus more BFs across 7 CONCLUSION

# print filter designs with nuanced properties. Many strive for better 8 ACKNOWLEDGMENT

# REFERENCES

to the LSM-tree’s merge overheads. On the other hand, Chucky’s                 internal counters [81]. Morton filter [15] maps entries to variable
performance draws nearer to the curve with disabled filters. The               sized “slots” within larger fixed-sized “blocks” and can therefore
reason is that it only updates an entry’s LID as an entry moves from           accommodate variable-sized entries more gracefully. Integrating
one level to the next, and with larger size ratios there are fewer             Chucky with these filters to harness their properties can make for
levels in the LSM-tree. Hence, under greedy merge policies, which              intriguing future work.
are commonly used to optimize for range reads (e.g., the default               Range Filters. Recent LSM-tree designs use a range filter for each
setting in RocksDB), Chucky tangibly improves end-to-end writes.               run [73, 100], which can save storage I/Os for range reads but
Throughput Scalability. Figure 14 Part (H) shows how through-                  require more memory I/Os to access and construct. Applying design
put scales as we increase the data size for a workload consisting of           elements from Chucky to create a unified range filter over a whole
95% Zipfian reads and 5% Zipfian writes (modeled after Workload B              LSM-tree to reduce memory I/Os is an intriguing future direction.
in YCSB [23]). Since writes are skewed, newer updates rapidly                  Learned Fence Pointers. Recent work attempts to reduce the
replace older entries during compaction at smaller levels and so               fence pointers’ memory I/O overheads through learned indexes [24]
major compaction and filter resizing do not take place. The BF base-           by extrapolating an entry’s location within a run based on the data’s
lines do not scale well as they issue memory I/Os across a growing             key distribution. Such work can complement Chucky by addressing
number of BFs. Chucky with uncompressed LIDs also exhibits dete-               the fence pointers, which become the next memory I/O bottleneck
riorating performance as its FPR grows and leads to more storage               once Chucky is applied.
I/Os. Chucky with compressed LIDs also exhibits deteriorating per-             Learning from Negative Queries. Recent filtering approaches
formance, mostly because the of the growing cost of the binary                 have been devised to learn from commonly issued negative queries
search across the fence pointers. However, Chucky provides better              (to non-existing keys) to reduce the false positive rate [32, 65, 78].
throughput with data size than all baselines because it scales the             Integrating such techniques with Chucky is an intriguing direction.
filter’s FPR and memory I/Os at the same time.                                 Bloom Filters (BF). Numerous BF variants have been proposed
                                                                               [16, 72, 93], which enable counting [13, 40, 89], compressibility [77],
6 RELATED WORK                                                                 vectoriziation [83], deletes for some but not all entries [88], efficient
                                                                               hashing [33, 60] and cache locality [17, 31, 66, 67, 84]. Bloomier
LSM-Tree Performance. With LSM-tree being adapted as a stor-                   filter allows to associate values with keys but is unable to compress
age engine across many systems (e.g., Cassandra [3], HBase [4],                values and is more complicated than fingerprint filters [19, 20].
AsterixDB [2], RocksDB [35, 38, 74]), there is significant interest            Entropy Coding. Aside to Huffman coding, there exist other meth-
in optimizing LSM-tree performance [48, 70]. Most designs to date              ods for compressing alphabets based on the probability distribution
focus on managing compaction overheads, for example by sepa-                   of the constituent symbols: Arithmetic Coding [82, 87] and Asym-
rating values from keys [18, 68], partitioning runs into files and             metric Numeral Systems [36]. These methods do not require the
merging based on maximal file intersections [7, 92, 94], keeping hot           use of auxiliary structures for encoding or decoding symbols. Har-
entries in the buffer [7], making the buffer more dense [14] or con-           nessing such techniques to eliminate Chucky’s auxiliary structures
current [42], scheduling carefully to prevent tail latency [8, 69, 91],        (i.e., the Huffman Tree, the Decoding Table, and the Recoding table)
using customized or dedicated hardware [1, 45, 95, 97, 101], and by            is an interesting future direction.
controlling delete persistence [90].
                                                                               7 CONCLUSION
   Another strain of work uses lazier compaction policies [76, 85,
86, 98, 99], which result in more runs and thus more BFs across                This paper that shows that as the performance gap between SSDs
which false positives and memory I/Os take place. Several works                and memory devices is shrinking, the Bloom filters of LSM-tree
show how to implement lazier merge policies while still keeping                are becoming a memory access bottleneck. We therefore propose
the FPR modest, but they still incur many memory I/Os across many              Chucky, a filter for LSM-tree that requires fewer memory I/Os to
BFs [25, 28, 29, 49–51]. SlimDB [86] shows how to reduce memory                query and maintain than Bloom filters. Chucky uses a Cuckoo filter
I/Os using a Cuckoo filter, but its memory footprint does not scale            in memory to map all entries to their locations within the LSM-
well as discussed in Section 3. In contrast, we show how to scale the          tree, and it compresses this location information to keep the false
FPR, memory I/Os and memory footprint at the same time (for any                positive rate low and stable. Thus, Chucky achieves the best of all
merge policy including lazy ones) by replacing the Bloom filters by            worlds: fewer memory I/Os and a low and stable false positive rate,
a Cuckoo filter with compressed level IDs.                                     all for the same memory budget.
Fingerprint Filters. While we build Chucky on top of Cuckoo
filter [39] for its design simplicity, there exist many other finger-          8 ACKNOWLEDGMENT
print filter designs with nuanced properties. Many strive for better
cache locality by using linear probing [9, 81], biasing Cuckoo in-             We thank the anonymous Reviewers for their invaluable feedback.
sertions to one bucket [15], or by ensuring both candidate buckets             We also thank the entire Pliops team for facilitating this work.
are physically close [96]. Vacuum filter offers better memory uti-
lization by allowing the filter size to not be a power of two [96].            REFERENCES
Some designs allow to delay resizing the filter by chaining overflow
filters [22, 96] or by sacrificing fingerprint bits [81]. Xor filter sup-         [1] Ahmad, M. Y., and Kemme, B. Compaction management in distributed key-value
ports a better FPR in exchange for higher construction time [44, 80].                  datastores. PVLDB 8, 8 (2015), 850–861.
Other designs prevent overflows due to duplicate insertions using
                                                                                  [2] Alsubaiee, S., Altowim, Y., Altwaijry, H., Behm, A., Borkar, V. R., Bu, Y.,
                                                                                       Carey, M. J., Cetindil, I., Cheelangi, M., Faraaz, K., Gabrielova, E., Grover,
                                                                                       R., Heilbron, Z., Kim, Y.-S., Li, C., Li, G., Ok, J. M., Onose, N., Pirzadeh, P.,

<!-- page: 13 kind: pdf_page route: static -->
Tsotras, V. J., Vernica, R., Wen, J., and Westmann, T. AsterixDB: A Scalable,                                             [36] Duda, J., Tahboub, K., Gadgil, N. J., and Delp, E. J. The use of asymmetric
      Open Source BDMS. PVLDB 7, 14 (2014), 1905–1916.                                                                                numeral systems as an accurate replacement for huffman coding. In Picture
 [3] Apache. Cassandra. http://cassandra.apache.org.                                                                                  Coding Symposium (PCS) (2015).
 [4] Apache. HBase. http://hbase.apache.org/ .
 [5] Athanassoulis, M., and Idreos, S. Design Tradeoffs of Data Access Methods.                                                 [37] Eisenman, A., Gardner, D., AbdelRahman, I., Axboe, J., Dong, S., Hazelwood,
      SIGMOD (2016).
 [6] Athanassoulis, M., Kester, M. S., Maas, L. M., Stoica, R., Idreos, S., Ailamaki,                                                 K., Petersen, C., Cidon, A., and Katti, S. Reducing dram footprint with nvm
      A., and Callaghan, M. Designing Access Methods: The RUM Conjecture. EDBT                                                        in facebook. In EuroSys (2018).
      (2016).                                                                                                                   [38] Facebook. RocksDB. https://github.com/facebook/rocksdb.
                                                                                                                                [39] Fan, B., Andersen, D. G., Kaminsky, M., and Mitzenmacher, M. Cuckoo
 [7] Balmau, O., Didona, D., Guerraoui, R., Zwaenepoel, W., Yuan, H., Arora,                                                          Filter: Practically Better Than Bloom. CoNEXT (2014).
                                                                                                                                [40] Fan, L., Cao, P., Almeida, J., and Broder, A. Z. Summary cache: A scalable
      A., Gupta, K., and Konka, P. TRIAD: Creating Synergies Between Memory,                                                          wide-area Web cache sharing protocol. IEEE/ACM Transactions on Networking 8,
      Disk and Log in Log Structured Key-Value Stores. USENIX ATC (2017).                                                             3 (2000), 281–293.
 [8] Balmau, O., Dinu, F., Zwaenepoel, W., Gupta, K., Chandhiramoorthi, R.,
      and Didona, D. {SILK}: Preventing latency spikes in log-structured merge                                                  [41] Gilad, E., Bortnikov, E., Braginsky, A., Gottesman, Y., Hillel, E., Keidar, I.,
      key-value stores. In USENIX ATC (2019).
 [9] Bender, M. A., Farach-Colton, M., Johnson, R., Kraner, R., Kuszmaul, B. C.,                                                      Moscovici, N., and Shahout, R. Evendb: Optimizing key-value storage for
                                                                                                                                      spatial locality. In EuroSys (2020).
      Medjedovic, D., Montes, P., Shetty, P., Spillane, R. P., and Zadok, E. Don’t                                              [42] Golan-Gueta, G., Bortnikov, E., Hillel, E., and Keidar, I. Scaling Concurrent
      Thrash: How to Cache Your Hash on Flash. PVLDB 5, 11 (2012), 1627–1637.                                                         Log-Structured Data Stores. EuroSys (2015).
[10] Bjørling, M., Bonnet, P., Bouganim, L., and Dayan, N. The Necessary Death                                                  [43] Golomb, S. Run-length encodings. IEEE transactions on information theory
      of the Block Device Interface. CIDR (2013).                                                                                     (1966).
[11] Bloom, B. H. Space/Time Trade-offs in Hash Coding with Allowable Errors.
      CACM 13, 7 (1970), 422–426.                                                                                               [44] Graf, T. M., and Lemire, D. Xor filters: Faster and smaller than bloom and
[12] Bonnet, P., and Dayan, N. Solid-state storage device flash translation layer,                                                    cuckoo filters. Journal of Experimental Algorithmics (2020).

2017. US Patent App. 15/056,381.                                                                                          [45] Huang, G., Cheng, X., Wang, J., Wang, Y., He, D., Zhang, T., Li, F., Wang,

[13] Bonomi, F., Mitzenmacher, M., Panigrahy, R., Singh, S., and Varghese, G.                                                         S., Cao, W., and Li, Q. X-engine: An optimized storage engine for large-scale
      An improved construction for counting bloom filters. In European Symposium                                                      e-commerce transaction processing. In SIGMOD (2019).
      on Algorithms (2006).                                                                                                     [46] Huffman, D. A. A Method for the Construction of Minimum-Redundancy
                                                                                                                                      Codes. Proceedings of the IRE 40, 9 (1952), 1098–1101.
[14] Bortnikov, E., Braginsky, A., Hillel, E., Keidar, I., and Sheffi, G. Accordion:                                            [47] Idreos, S., Athanassoulis, M., Dayan, N., Guo, D., Kester, M. S., Maas, L.,
      Better Memory Organization for LSM Key-Value Stores. PVLDB 11, 12 (2018),
      1863–1875.                                                                                                                      and Zoumpatianos, K. Past and future steps for adaptive storage data systems:
                                                                                                                                      From shallow to deep adaptivity. In Real-Time Business Intelligence and Analytics.
[15] Breslow, A. D., and Jayasena, N. S. Morton filters: faster, space-efficient                                                      Springer, 2015.
      cuckoo filters via biasing, compression, and decoupled logical sparsity. In VLDB                                          [48] Idreos, S., and Callaghan, M. Key-value storage engines. In SIGMOD (2020).
      (2018).                                                                                                                   [49] Idreos, S., and Dayan, N. File management with log-structured merge bush,

[16] Broder, A. Z., and Mitzenmacher, M. Network Applications of Bloom Filters:                                                       2020. US Patent App. 16/963,411.
      A Survey. Internet Mathematics 1 (2002), 636–646.
                                                                                                                                [50] Idreos, S., and Dayan, N. Key-value stores with optimized merge policies and
[17] Canim, M., Mihaila, G. A., Bhattacharjee, B., Ross, K. A., and Lang, C. A. SSD
      Bufferpool Extensions for Database Systems. PVLDB 3, 1-2 (2010), 1435–1446.                                                     optimized lsm-tree structures, 2020. US Patent App. 16/963,411.

[18] Chan, H. H. W., Li, Y., Lee, P. P. C., and Xu, Y. HashKV: Enabling Efficient                                               [51] Idreos, S., Dayan, N., and Athanassoulis, M. Optimized navigable key-value
      Updates in KV Storage via Hashing. ATC (2018).
                                                                                                                                      store, 2020. US Patent App. 16/433,075.
[19] Charles, D., and Chellapilla, K. Bloomier filters: A second look. In European
      Symposium on Algorithms (2008).                                                                                           [52] Idreos, S., Dayan, N., Qin, W., Akmanalp, M., Hilgard, S., Ross, A., Lennon,
                                                                                                                                      J., Jain, V., Gupta, H., Li, D., et al. Learning key-value store design. arXiv
[20] Chazelle, B., Kilian, J., Rubinfeld, R., and Tal, A. The bloomier filter: an                                                     preprint arXiv:1907.05443 (2019).
      efficient data structure for static support lookup tables. In Symposium on Discrete
      Algorithms (2004).                                                                                                        [53] Idreos, S., Dayan, N., Qin, W., Akmanalp, M., Hilgard, S., Ross, A., Lennon,

[21] Chen, G. J., Wiener, J. L., Iyer, S., Jaiswal, A., Lei, R., Simha, N., Wang, W.,                                                 J., Jain, V., Gupta, H., Li, D., and Zhu, Z. Design continuums and the path
                                                                                                                                      toward self-designing key-value stores that know and learn. In CIDR (2019).
      Wilfong, K., Williamson, T., and Yilmaz, S. Realtime data processing at                                                   [54] Idreos, S., and Kraska, T. From auto-tuning one size fits all to self-designed
      facebook. In SIGMOD (2016).                                                                                                     and learned data-intensive systems. In SIGMOD (2019).
[22] Chen, H., Liao, L., Jin, H., and Wu, J. The dynamic cuckoo filter. In IEEE ICNP                                            [55] Idreos, S., Zoumpatianos, K., Athanassoulis, M., Dayan, N., Hentschel,
      (2017).
                                                                                                                                      B., Kester, M. S., Guo, D., Maas, L. M., Qin, W., Wasay, A., and Sun, Y. The
[23] Cooper, B. F., Silberstein, A., Tam, E., Ramakrishnan, R., and Sears, R.                                                         Periodic Table of Data Structures. IEEE DEBULL 41, 3 (2018), 64–75.
      Benchmarking cloud serving systems with YCSB. SoCC (2010).                                                                [56] Idreos, S., Zoumpatianos, K., Chatterjee, S., Qin, W., Wasay, A., Hentschel,

[24] Dai, Y., Xu, Y., Ganesan, A., Alagappan, R., Kroth, B., Arpaci-Dusseau,                                                          B., Kester, M., Dayan, N., Guo, D., Kang, M., et al. Learning data structure
                                                                                                                                      alchemy. IEEE DEBULL (2019).
      A., and Arpaci-Dusseau, R. From wisckey to bourbon: A learned index for                                                   [57] Idreos, S., Zoumpatianos, K., Hentschel, B., Kester, M. S., and Guo, D. The
      log-structured merge trees. In USENIX OSDI (2020).
[25] Dayan, N., Athanassoulis, M., and Idreos, S. Monkey: Optimal Navigable                                                           Data Calculator: Data Structure Design and Cost Synthesis from First Principles
      Key-Value Store. SIGMOD (2017).                                                                                                 and Learned Cost Models. SIGMOD (2018).
[26] Dayan, N., Athanassoulis, M., and Idreos, S. Optimal Bloom Filters and                                                     [58] Im, J., Bae, J., Chung, C., Lee, S., et al. Pink: High-speed in-storage key-value
      Adaptive Merging for LSM-Trees. TODS 43, 4 (2018), 16:1–16:48.                                                                  store with bounded tails. In USENIX ATC (2020).
[27] Dayan, N., Bonnet, P., and Idreos, S. GeckoFTL: Scalable Flash Translation                                                 [59] Jagadish, H. V., Narayan, P. P. S., Seshadri, S., Sudarshan, S., and Kan-
      Techniques For Very Large Flash Devices. SIGMOD (2016).
[28] Dayan, N., and Idreos, S. Dostoevsky: Better Space-Time Trade-Offs for LSM-                                                      neganti, R. Incremental Organization for Data Recording and Warehousing.
                                                                                                                                      VLDB (1997).
      Tree Based Key-Value Stores via Adaptive Removal of Superfluous Merging.                                                  [60] Kirsch, A., and Mitzenmacher, M. Less hashing, same performance: Building
      SIGMOD (2018).                                                                                                                  a better Bloom filter. Random Structures & Algorithms 33, 2 (2008), 187–218.
[29] Dayan, N., and Idreos, S. The log-structured merge-bush & the wacky contin-                                                [61] Kondylakis, H., Dayan, N., Zoumpatianos, K., and Palpanas, T. Coconut: A
      uum. In SIGMOD (2019).                                                                                                          scalable bottom-up approach for building data series indexes. VLDB 11, 6 (2018),
[30] Dayan, N., Svendsen, M. K., Bjorling, M., Bonnet, P., and Bouganim, L.                                                           677–690.
      Eagletree: exploring the design space of ssd-based algorithms. VLDB (2013).
[31] Debnath, B., Sengupta, S., Li, J., Lilja, D. J., and Du, D. H. Bloomflash: Bloom                                           [62] Kondylakis, H., Dayan, N., Zoumpatianos, K., and Palpanas, T. Coconut
      filter on flash-based storage. In ICDCS (2011).                                                                                 palm: Static and streaming data series exploration now in your palm. In SIGMOD
[32] Deeds, K., Hentschel, B., and Idreos, S. Stacked filters: learning to filter by                                                  (2019).
      structure. PVLDB (2020).
[33] Dillinger, P. C., and Manolios, P. Bloom Filters in Probabilistic Verification.                                            [63] Kondylakis, H., Dayan, N., Zoumpatianos, K., and Palpanas, T. Coconut:
      Formal Methods in Computer-Aided Design (2004).
[34] Dinh, T. T. A., Wang, J., Chen, G., Liu, R., Ooi, B. C., and Tan, K.-L. Blockbench:                                              sortable summarizations for scalable indexes over static and streaming data
      A framework for analyzing private blockchains. In SIGMOD (2017).                                                                series. VLDBJ (2019).
[35] Dong, S., Callaghan, M., Galanis, L., Borthakur, D., Savor, T., and Strum,                                                 [64] Kraft, L. G. A device for quantizing, grouping, and coding amplitude-modulated
      M. Optimizing Space Amplification in RocksDB. CIDR (2017).                                                                      pulses. PhD thesis, MIT, 1949.
                                                                                                                                [65] Kraska, T., Beutel, A., Chi, E. H., Dean, J., and Polyzotis, N. The Case for
                                                                                                                            13        Learned Index Structures. SIGMOD (2018).
                                                                                                                                [66] Lang, H., Neumann, T., Kemper, A., and Boncz, P. Performance-optimal
                                                                                                                                      filtering: Bloom overtakes cuckoo at high throughput. In VLDB (2019).
                                                                                                                                [67] Lu, G., Debnath, B., and Du, D. H. C. A Forest-structured Bloom Filter with
                                                                                                                                      flash memory. MSST (2011).

<!-- page: 14 kind: pdf_page route: static -->
[68] Lu, L., Pillai, T. S., Arpaci-Dusseau, A. C., and Arpaci-Dusseau, R. H. Wis-            [86] Ren, K., Zheng, Q., Arulraj, J., and Gibson, G. SlimDB: A Space-Efficient Key-
      cKey: Separating Keys from Values in SSD-conscious Storage. FAST (2016).                     Value Storage Engine For Semi-Sorted Data. PVLDB 10, 13 (2017), 2037–2048.

[69] Luo, C., and Carey, M. J. On performance stability in lsm-based storage systems.        [87] Rissanen, J., and Langdon, G. G. Arithmetic coding. IBM Journal of research
      In VLDB (2019).                                                                              and development (1979).

[70] Luo, C., and Carey, M. J. Lsm-based storage techniques: a survey. The VLDB              [88] Rothenberg, C. E., Macapuna, C., Verdi, F., and Magalhaes, M. The deletable
      Journal (2020).                                                                              Bloom filter: a new member of the Bloom family. IEEE Communications Letters
                                                                                                   14, 6 (jun 2010), 557–559.
[71] Luo, C., Tözün, P., Tian, Y., Barber, R., Raman, V., and Sidle, R. Umzi: Unified
      multi-zone indexing for large-scale htap. In EDBT (2019).                              [89] Rottenstreich, O., Kanizo, Y., and Keslassy, I. The variable-increment
                                                                                                   counting bloom filter. IEEE/ACM Transactions on Networking (2013).
[72] Luo, L., Guo, D., Ma, R. T., Rottenstreich, O., and Luo, X. Optimizing bloom
      filter: Challenges, solutions, and comparisons. IEEE Commun. Surv. Tutor. (2018).      [90] Sarkar, S., Papon, T. I., Staratzis, D., and Athanassoulis, M. Lethe: A tunable
                                                                                                   delete-aware lsm engine. In SIGMOD (2020).
[73] Luo, S., Chatterjee, S., Ketsetsidis, R., Dayan, N., Qin, W., and Idreos, S.
                                                                                             [91] Sears, R., and Ramakrishnan, R. bLSM: A General Purpose Log Structured
      Rosetta: A robust space-time optimized range filter for key-value stores. In                 Merge Tree. SIGMOD (2012).
      SIGMOD (2020).
[74] Matsunobu, Y., Dong, S., and Lee, H. Myrocks: Lsm-tree database storage                 [92] Shetty, P., Spillane, R. P., Malpani, R., Andrews, B., Seyster, J., and Zadok,
      engine serving facebook’s social graph. VLDB (2020).                                         E. Building Workload-Independent Storage with VT-trees. FAST (2013).
[75] McMillan, B. Two inequalities implied by unique decipherability. IRE Transac-
      tions on Information Theory (1956).                                                    [93] Tarkoma, S., Rothenberg, C. E., and Lagerspetz, E. Theory and Practice of
[76] Mei, F., Cao, Q., Jiang, H., and Li, J. Sifrdb: A unified solution for write-                 Bloom Filters for Distributed Systems. IEEE Communications Surveys & Tutorials
      optimized key-value stores in large datacenter. In ACM SOCC (2018).                          14, 1 (2012), 131–155.
[77] Mitzenmacher, M. Compressed bloom filters. IEEE/ACM Transactions on
      Networking (2002).                                                                     [94] Thonangi, R., and Yang, J. On Log-Structured Merge for Solid-State Drives.
[78] Mitzenmacher, M., Pontarelli, S., and Reviriego, P. Adaptive cuckoo filters.                  ICDE (2017).
      In SIAM ALENEX (2018).
[79] O’Neil, P. E., Cheng, E., Gawlick, D., and O’Neil, E. J. The log-structured             [95] Vinçon, T., Hardock, S., Riegger, C., Oppermann, J., Koch, A., and Petrov, I.
      merge-tree (LSM-tree). Acta Informatica 33, 4 (1996), 351–385.
[80] Pagh, A., Pagh, R., and Rao, S. S. An optimal bloom filter replacement. In                    Noftl-kv: Tackling write-amplification on kv-stores with native storage man-
      SODA (2005).                                                                                 agement. In EDBT (2018).
[81] Pandey, P., Bender, M. A., Johnson, R., and Patro, R. A general-purpose                 [96] Wang, M., Zhou, M., Shi, S., and Qian, C. Vacuum filters: more space-efficient
      counting filter: Making every bit count. In SIGMOD (2017).                                   and faster replacement for bloom and cuckoo filters. In VLDB (2019).
[82] Pasco, R. C. Source coding algorithms for fast data compression. PhD thesis,            [97] Wang, P., Sun, G., Jiang, S., Ouyang, J., Lin, S., Zhang, C., and Cong, J. An
      Stanford University CA, 1976.
                                                                                              [98]  Efficient Design and Implementation of LSM-Tree based Key-Value Store on
[83] Polychroniou, O., and Ross, K. A. Vectorized Bloom filters for advanced SIMD             [99]  Open-Channel SSD. EuroSys (2014).
      processors. DAMON (2014).                                                              [100]  Wu, X., Xu, Y., Shao, Z., and Jiang, S. LSM-trie: An LSM-tree-based Ultra-Large
                                                                                                    Key-Value Store for Small Data Items. USENIX ATC (2015).
[84] Putze, F., Sanders, P., and Singler, J. Cache-, hash-, and space-efficient bloom               Yao, T., Wan, J., Huang, P., He, X., Wu, F., and Xie, C. Building Efficient Key-
      filters. Journal of Experimental Algorithmics (JEA) (2010).                                   Value Stores via a Lightweight Compaction Tree. TOS 13, 4 (2017), 29:1–29:28.
                                                                                                    Zhang, H., Lim, H., Leis, V., Andersen, D. G., Kaminsky, M., Keeton, K.,
[85] Raju, P., Kadekodi, R., Chidambaram, V., and Abraham, I. PebblesDB: Building
      Key-Value Stores using Fragmented Log-Structured Merge Trees. SOSP (2017).                     and Pavlo, A. SuRF: Practical Range Query Filtering with Fast Succinct Tries.
                                                                                                     SIGMOD (2018).
                                                                                             [101] Zhang, T., Wang, J., Cheng, X., Xu, H., Yu, N., Huang, G., Zhang, T., He, D.,

Li, F., Cao, W., et al. Fpga-accelerated compactions for lsm-based key-value
                                                                                                    store. In USENIX FAST (2020).

