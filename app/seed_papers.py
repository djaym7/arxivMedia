"""Curated set of landmark arXiv papers used to backfill the corpus.

These are well-known, genuinely high-citation works across cs.CL / cs.LG /
cs.AI / cs.CV. Seeding them gives the feed real "Most Cited" heavyweights so the
citation badges show large, true numbers (fetched live from OpenAlex), instead
of a corpus made entirely of day-old preprints with zero citations.

Each entry is just a bare arXiv id; all metadata (title, abstract, url) is
fetched from the arXiv API at seed time. The list is deduplicated by source on
insert (posts.source is UNIQUE as 'arxiv:<id>'), so seeding is idempotent.
"""

# Bare arXiv ids (no version suffix). Breadth over depth; ~70 landmarks.
SEED_ARXIV_IDS: list[str] = [
    # Transformers / attention / seq2seq
    "1706.03762",  # Attention Is All You Need
    "1409.3215",   # Sequence to Sequence Learning with Neural Networks
    "1409.0473",   # Neural Machine Translation by Jointly Learning to Align and Translate
    "1508.04025",  # Effective Approaches to Attention-based NMT (Luong)
    # Pretrained language models
    "1810.04805",  # BERT
    "1907.11692",  # RoBERTa
    "1910.10683",  # T5
    "1909.11942",  # ALBERT
    "1910.01108",  # DistilBERT
    "2005.14165",  # GPT-3 (Language Models are Few-Shot Learners)
    "2302.13971",  # LLaMA
    "2307.09288",  # Llama 2
    "2203.02155",  # InstructGPT (Training LMs to follow instructions w/ human feedback)
    "2201.11903",  # Chain-of-Thought Prompting
    "2005.11401",  # RAG (Retrieval-Augmented Generation)
    "1908.10084",  # Sentence-BERT
    "1301.3781",   # word2vec (Efficient Estimation of Word Representations)
    "1310.4546",   # word2vec (Distributed Representations / negative sampling)
    "1607.04606",  # fastText (Enriching Word Vectors with Subword Information)
    "1802.05365",  # ELMo (Deep contextualized word representations)
    "1910.13461",  # BART
    "1901.02860",  # Transformer-XL
    "2009.06732",  # Efficient Transformers: A Survey
    # Optimization / training / regularization
    "1412.6980",   # Adam
    "1502.03167",  # Batch Normalization
    "1607.06450",  # Layer Normalization
    "1207.0580",   # Dropout (Improving NNs by preventing co-adaptation)
    "1502.01852",  # PReLU / He initialization (Delving Deep into Rectifiers)
    "1609.04747",  # An overview of gradient descent optimization algorithms
    "1608.03983",  # SGDR (warm restarts)
    "1711.05101",  # Decoupled Weight Decay Regularization (AdamW)
    "1503.02531",  # Knowledge Distillation
    "1611.03530",  # Understanding deep learning requires rethinking generalization
    "1810.00826",  # How Powerful are Graph Neural Networks? (GIN)
    # Vision / CNNs / detection / segmentation
    "1512.03385",  # ResNet (Deep Residual Learning)
    "1409.1556",   # VGG (Very Deep Convolutional Networks)
    "1409.4842",   # GoogLeNet / Inception
    "1608.06993",  # DenseNet
    "1602.07360",  # SqueezeNet
    "1704.04861",  # MobileNets
    "1801.04381",  # MobileNetV2
    "1905.11946",  # EfficientNet
    "1311.2524",   # R-CNN (Rich feature hierarchies)
    "1504.08083",  # Fast R-CNN
    "1506.01497",  # Faster R-CNN
    "1506.02640",  # YOLO (You Only Look Once)
    "1512.02325",  # SSD (Single Shot MultiBox Detector)
    "1703.06870",  # Mask R-CNN
    "1505.04597",  # U-Net
    "1411.4038",   # Fully Convolutional Networks for Semantic Segmentation
    "2010.11929",  # Vision Transformer (ViT)
    "2103.14030",  # Swin Transformer
    "2005.12872",  # DETR (End-to-End Object Detection with Transformers)
    # Generative models
    "1406.2661",   # GAN (Generative Adversarial Networks)
    "1511.06434",  # DCGAN
    "1701.07875",  # Wasserstein GAN
    "1812.04948",  # StyleGAN (A Style-Based Generator)
    "1703.10593",  # CycleGAN
    "1611.07004",  # pix2pix (Image-to-Image Translation w/ Conditional GANs)
    "1312.6114",   # VAE (Auto-Encoding Variational Bayes)
    "2006.11239",  # DDPM (Denoising Diffusion Probabilistic Models)
    "2112.10752",  # Latent Diffusion / Stable Diffusion
    # Multimodal
    "2103.00020",  # CLIP (Learning Transferable Visual Models From NL Supervision)
    "2102.12092",  # DALL-E (Zero-Shot Text-to-Image Generation)
    # Reinforcement learning
    "1312.5602",   # DQN (Playing Atari with Deep RL)
    "1509.02971",  # DDPG (Continuous control with deep RL)
    "1707.06347",  # PPO (Proximal Policy Optimization)
    "1602.01783",  # A3C (Asynchronous Methods for Deep RL)
    "1707.06887",  # Distributional RL (C51)
    # Graph / misc foundational
    "1609.02907",  # GCN (Semi-Supervised Classification with Graph Convolutional Networks)
    "1710.10903",  # Graph Attention Networks (GAT)
    "1412.3555",   # Empirical Evaluation of Gated RNNs (GRU)
    "1308.0850",   # Generating Sequences With RNNs (Graves)
]
